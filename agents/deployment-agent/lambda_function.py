import os
import json
import math
import statistics
import time
import logging
import psycopg2

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def estimate_rotation_deg(trials):
    sin_sum = 0.0
    cos_sum = 0.0
    for t in trials:
        error_rad = math.radians(t["decoded"] - t["intended"])
        sin_sum += math.sin(error_rad)
        cos_sum += math.cos(error_rad)
    mean_error_rad = math.atan2(sin_sum, cos_sum)
    return math.degrees(mean_error_rad)

def build_feature_vector(readings):
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
    try:
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": "DB connection failed"})}

    cursor = conn.cursor()
    sessions_processed = 0
    deployments_completed = 0

    try:
        # 1. Query deploying sessions
        cursor.execute("SELECT session_id, patient_id, version FROM session_state WHERE state = 'deploying'")
        deploying_sessions = cursor.fetchall()

        for session_row in deploying_sessions:
            session_id, patient_id, version = session_row
            sessions_processed += 1
            start_time = time.time()

            try:
                # 2. Claim the session
                cursor.execute("""
                    UPDATE session_state 
                    SET state='executing_deployment', locked_by_agent='deployment_agent', version=version+1, updated_at=now() 
                    WHERE session_id=%s AND version=%s
                """, (session_id, version))

                if cursor.rowcount == 0:
                    cursor.execute("""
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, ('deployment_agent', 'execute_deployment', session_id, 'claim_conflict_skipped', 0))
                    conn.commit()
                    continue

                new_version = version + 1

                # 3. Fetch approved params
                cursor.execute("""
                    SELECT final_params FROM approval_events 
                    WHERE session_id = %s AND decision = 'approve' 
                    ORDER BY ts DESC LIMIT 1
                """, (session_id,))
                approval_row = cursor.fetchone()

                if not approval_row:
                    latency = int((time.time() - start_time) * 1000)
                    cursor.execute("""
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, ('deployment_agent', 'execute_deployment', session_id, 'no_approval_found', latency))
                    conn.commit()
                    continue

                final_params_raw = approval_row[0]
                final_params = final_params_raw if isinstance(final_params_raw, dict) else json.loads(final_params_raw)

                # 4. Fetch telemetry
                cursor.execute("""
                    SELECT angle_error, kl_divergence, channel_metrics, trials 
                    FROM telemetry_events 
                    WHERE session_id = %s 
                    ORDER BY ts DESC LIMIT 5
                """, (session_id,))
                telemetry_rows = cursor.fetchall()

                readings = []
                trials_list = None

                for r in telemetry_rows:
                    angle_err, kl_div, c_metrics_raw, trials_raw = r
                    c_metrics = c_metrics_raw if isinstance(c_metrics_raw, dict) else json.loads(c_metrics_raw)
                    
                    readings.append({
                        'angle_error': angle_err,
                        'kl_divergence': kl_div,
                        'channel_metrics': c_metrics
                    })

                    if trials_list is None and trials_raw is not None:
                        t_parsed = trials_raw if isinstance(trials_raw, dict) else json.loads(trials_raw)
                        if "trials" in t_parsed and t_parsed["trials"]:
                            trials_list = t_parsed["trials"]

                if trials_list is None or not readings:
                    latency = int((time.time() - start_time) * 1000)
                    cursor.execute("""
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, ('deployment_agent', 'execute_deployment', session_id, 'no_trial_data', latency))
                    conn.commit()
                    continue

                # 5. Compute pre_residual_deg
                pre_residual_deg = estimate_rotation_deg(trials_list)

                # 6. Get rotation correction
                rotation_correction = float(final_params.get("rotation_deg", 0.0))

                # 7. Build corrected trials and compute post_residual_deg
                corrected_trials = []
                for t in trials_list:
                    corrected_trials.append({
                        "intended": t["intended"],
                        "decoded": (t["decoded"] + rotation_correction) % 360
                    })
                post_residual_deg = estimate_rotation_deg(corrected_trials)

                # 8. Compute outcome_score
                outcome_score = round(max(0.3, min(0.98, 1 - abs(post_residual_deg) / 30.0)), 3)

                # 9. Build metrics JSON
                pre_metrics = {"pre_residual_deg": round(pre_residual_deg, 2)}
                post_metrics = {"post_residual_deg": round(post_residual_deg, 2), "outcome_score": outcome_score}

                # 10. Insert deployment event
                cursor.execute("""
                    INSERT INTO deployment_events (session_id, applied_params, pre_metrics, post_metrics) 
                    VALUES (%s, %s, %s, %s) RETURNING deployment_id
                """, (session_id, json.dumps(final_params), json.dumps(pre_metrics), json.dumps(post_metrics)))
                deployment_id = cursor.fetchone()[0]

                # 11. Determine root_cause_label
                root_cause_label = "novel_pattern_resolved"
                cursor.execute("""
                    SELECT retrieved_signature_ids FROM diagnostic_records 
                    WHERE session_id = %s ORDER BY created_at DESC LIMIT 1
                """, (session_id,))
                diag_row = cursor.fetchone()

                if diag_row and diag_row[0]:
                    sig_ids_raw = diag_row[0]
                    if isinstance(sig_ids_raw, str):
                        stripped = sig_ids_raw.strip('{}')
                        sig_ids_list = stripped.split(',') if stripped else []
                    else:
                        sig_ids_list = sig_ids_raw

                    if sig_ids_list and len(sig_ids_list) > 0:
                        first_sig_id = sig_ids_list[0]
                    cursor.execute("SELECT root_cause_label FROM drift_signatures WHERE signature_id = %s", (first_sig_id,))
                    rc_row = cursor.fetchone()
                    if rc_row and rc_row[0]:
                        root_cause_label = rc_row[0]

                # 12. Build the new embedding
                embedding_vector = build_feature_vector(readings)
                embedding_str = f"[{','.join(map(str, embedding_vector))}]"

                # 13. Insert the new memory
                mean_angle_err = statistics.mean([r['angle_error'] for r in readings])
                mean_kl_div = statistics.mean([r['kl_divergence'] for r in readings])
                feature_summary = json.dumps({
                    "mean_angle_error": mean_angle_err,
                    "mean_kl_divergence": mean_kl_div
                })

                cursor.execute("""
                    INSERT INTO drift_signatures 
                    (patient_id, embedding, feature_summary, root_cause_label, resolved_params, source_deployment, outcome_score) 
                    VALUES (%s, %s::VECTOR, %s, %s, %s, %s, %s)
                """, (patient_id, embedding_str, feature_summary, root_cause_label, json.dumps(final_params), deployment_id, outcome_score))

                # 14. Transition session forward
                cursor.execute("""
                    UPDATE session_state 
                    SET state='resolved', version=version+1, updated_at=now() 
                    WHERE session_id=%s AND version=%s
                """, (session_id, new_version))

                # 15. Insert success into audit log
                latency = int((time.time() - start_time) * 1000)
                cursor.execute("""
                    INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                    VALUES (%s, %s, %s, %s, %s)
                """, ('deployment_agent', 'execute_deployment', session_id, 'success', latency))

                # 16. Commit the session
                conn.commit()
                deployments_completed += 1

            except Exception as session_error:
                conn.rollback()
                logger.error(f"Error processing session {session_id}: {session_error}")
                latency = int((time.time() - start_time) * 1000)
                try:
                    cursor.execute("""
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, ('deployment_agent', 'execute_deployment', session_id, 'error', latency))
                    conn.commit()
                except Exception as audit_error:
                    conn.rollback()
                    logger.error(f"Failed to record error in agent_audit_log for session {session_id}: {audit_error}")
                continue

    finally:
        cursor.close()
        conn.close()

    # 17. Return summary
    return {
        "statusCode": 200,
        "body": {
            "sessions_processed": sessions_processed,
            "deployments_completed": deployments_completed
        }
    }