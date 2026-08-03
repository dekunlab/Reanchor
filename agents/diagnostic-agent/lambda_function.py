import psycopg2
import boto3
import os
import json
import statistics
import time
import logging

# Configure basic logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Constants
NOVELTY_THRESHOLD = 0.6

# Initialize Bedrock client
bedrock_client = boto3.client("bedrock-runtime", region_name="eu-north-1")

def build_feature_vector(readings):
    import statistics
    angle_errors = [r['angle_error'] for r in readings]
    kls = [r['kl_divergence'] for r in readings]
    mean_angle = statistics.mean(angle_errors) / 40.0
    std_angle = (statistics.stdev(angle_errors) / 20.0) if len(angle_errors) > 1 else 0.0
    min_angle = min(angle_errors) / 40.0
    max_angle = max(angle_errors) / 40.0
    mean_kl = statistics.mean(kls) / 1.0
    std_kl = (statistics.stdev(kls) / 0.5) if len(kls) > 1 else 0.0
    min_kl = min(kls) / 1.0
    max_kl = max(kls) / 1.0
    channel_devs = []
    for i in range(1, 9):
        vals = [r['channel_metrics'][f'channel_{i}'] for r in readings]
        avg_dev = statistics.mean([abs(v - 1.0) for v in vals]) / 0.5
        channel_devs.append(avg_dev)
    vector = [mean_angle, std_angle, min_angle, max_angle, mean_kl, std_kl, min_kl, max_kl] + channel_devs
    return [max(0.0, min(1.0, x)) for x in vector]

def lambda_handler(event, context):
    sessions_processed = 0
    diagnoses_written = 0
    
    conn = None
    cur = None
    
    try:
        cert_path = os.path.join(os.path.dirname(__file__), "root.crt")
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=cert_path
        )
        cur = conn.cursor()
        
        # 1. Query for anomaly sessions
        cur.execute(
            "SELECT session_id, patient_id, version FROM session_state WHERE state = 'anomaly_detected'"
        )
        sessions = cur.fetchall()
        
        for session in sessions:
            session_id, patient_id, version = session
            start_time = time.time()
            sessions_processed += 1
            
            try:
                # 2. Claim the session
                cur.execute(
                    """
                    UPDATE session_state 
                    SET state = 'diagnosing', locked_by_agent = 'diagnostic_agent', 
                        version = version + 1, updated_at = now() 
                    WHERE session_id = %s AND version = %s
                    """,
                    (session_id, version)
                )
                
                if cur.rowcount == 0:
                    # Lost the race
                    latency_ms = int((time.time() - start_time) * 1000)
                    cur.execute(
                        """
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES ('diagnostic_agent', 'generate_diagnosis', %s, 'claim_conflict_skipped', %s)
                        """,
                        (session_id, latency_ms)
                    )
                    conn.commit()
                    continue
                
                # We successfully claimed the session; new version is version + 1
                new_version = version + 1
                
                # 3. Fetch the last 5 telemetry events
                cur.execute(
                    """
                    SELECT angle_error, kl_divergence, channel_metrics 
                    FROM telemetry_events 
                    WHERE session_id = %s 
                    ORDER BY ts DESC 
                    LIMIT 5
                    """,
                    (session_id,)
                )
                telemetry_rows = cur.fetchall()
                
                if not telemetry_rows:
                    conn.commit()
                    continue
                
                readings = []
                for row in telemetry_rows:
                    channel_metrics = row[2] if isinstance(row[2], dict) else json.loads(row[2])
                    readings.append({
                        'angle_error': row[0],
                        'kl_divergence': row[1],
                        'channel_metrics': channel_metrics
                    })
                
                # Build feature vector
                embedding_vector = build_feature_vector(readings)
                embedding_str = "[" + ",".join(map(str, embedding_vector)) + "]"
                
                # 4. Vector search against past memories
                cur.execute(
                    """
                    SELECT signature_id, root_cause_label, feature_summary, resolved_params, outcome_score, 
                           embedding <-> %s::VECTOR AS distance 
                    FROM drift_signatures 
                    WHERE patient_id = %s 
                    ORDER BY distance 
                    LIMIT 5
                    """,
                    (embedding_str, patient_id)
                )
                memories = cur.fetchall()
                
                # 5. Compute best_distance and confidence
                best_distance = memories[0][5] if memories else None
                
                if best_distance is not None:
                    confidence = max(0.3, min(0.95, 1.0 - best_distance))
                else:
                    confidence = 0.3
                    
                # 6. Build the prompt for Bedrock
                avg_angle = statistics.mean([r['angle_error'] for r in readings])
                avg_kl = statistics.mean([r['kl_divergence'] for r in readings])
                
                prompt_text = f"The patient's current BCI telemetry shows an average angle error of {avg_angle:.2f} and KL divergence of {avg_kl:.3f}.\n\n"
                
                if best_distance is None or best_distance > NOVELTY_THRESHOLD:
                    prompt_text += (
                        "This appears to be a novel pattern with no strong precedent in the patient's history. "
                        "Describe only what the current telemetry shows in a concise 2-3 sentence clinical hypothesis regarding the potential drift cause."
                    )
                else:
                    prompt_text += "Relevant past incidents for this patient:\n"
                    for m in memories:
                        prompt_text += f"- Likely Cause: {m[1]}, Outcome Score (efficacy of past fix): {m[4]:.2f}, Past Telemetry Snapshot: {json.dumps(m[2])}\n"
                    prompt_text += (
                        "\nWrite a concise 2-3 sentence clinical hypothesis about the likely cause of the current drift, "
                        "explicitly referencing which past pattern it most resembles and evaluating its severity based on the telemetry provided."
                    )
                    
                # 7. Call Amazon Bedrock
                bedrock_model_id = os.environ["BEDROCK_MODEL_ID"]
                response = bedrock_client.converse(
                    modelId=bedrock_model_id,
                    messages=[{"role": "user", "content": [{"text": prompt_text}]}]
                )
                hypothesis_text = response["output"]["message"]["content"][0]["text"]
                
                # 8. Insert into diagnostic_records
                retrieved_signature_ids = [m[0] for m in memories]
                cur.execute(
                    """
                    INSERT INTO diagnostic_records (session_id, hypothesis, confidence, retrieved_signature_ids, model_used)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (session_id, hypothesis_text, confidence, retrieved_signature_ids, bedrock_model_id)
                )
                
                # 9. Transition session state forward
                cur.execute(
                    """
                    UPDATE session_state 
                    SET state = 'calibrating', version = version + 1, updated_at = now() 
                    WHERE session_id = %s AND version = %s
                    """,
                    (session_id, new_version)
                )
                
                # 10. Insert into agent_audit_log
                latency_ms = int((time.time() - start_time) * 1000)
                cur.execute(
                    """
                    INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                    VALUES ('diagnostic_agent', 'generate_diagnosis', %s, 'success', %s)
                    """,
                    (session_id, latency_ms)
                )
                
                # 11. Commit transaction for this session
                conn.commit()
                diagnoses_written += 1
                
            except Exception as session_err:
                conn.rollback()
                logger.error("Error processing session %s: %s", session_id, str(session_err))
                continue
                
        # 12. Return the success summary
        return {
            "statusCode": 200,
            "body": {
                "sessions_processed": sessions_processed,
                "diagnoses_written": diagnoses_written
            }
        }
        
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("Global execution error: %s", str(e))
        return {
            "statusCode": 500,
            "body": {
                "error": str(e)
            }
        }
        
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()