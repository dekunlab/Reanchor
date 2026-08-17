import psycopg2
import os
import json
import math
import time
import logging

# Configure basic logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Constants
NOVELTY_THRESHOLD = 0.6

def estimate_rotation_deg(trials):
    """
    Genuine closed-form 2D Procrustes rotation fitting.
    Calculates the exact angular drift using circular statistics.
    """
    sin_sum = 0.0
    cos_sum = 0.0
    for t in trials:
        error_rad = math.radians(t["decoded"] - t["intended"])
        sin_sum += math.sin(error_rad)
        cos_sum += math.cos(error_rad)
    mean_error_rad = math.atan2(sin_sum, cos_sum)
    return math.degrees(mean_error_rad)

def lambda_handler(event, context):
    sessions_processed = 0
    proposals_written = 0
    
    conn = None
    cur = None
    
    try:
        # Establish database connection
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
        cur = conn.cursor()
        
        # 1. Query for calibrating sessions
        cur.execute(
            "SELECT session_id, patient_id, version FROM session_state WHERE state = 'calibrating'"
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
                    SET state = 'computing_calibration', locked_by_agent = 'calibration_agent', 
                        version = version + 1, updated_at = now() 
                    WHERE session_id = %s AND version = %s
                    """,
                    (session_id, version)
                )
                
                if cur.rowcount == 0:
                    # Claim conflict, another agent grabbed it
                    latency_ms = int((time.time() - start_time) * 1000)
                    cur.execute(
                        """
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES ('calibration_agent', 'compute_calibration', %s, 'claim_conflict_skipped', %s)
                        """,
                        (session_id, latency_ms)
                    )
                    conn.commit()
                    continue
                
                new_version = version + 1
                
                # 3. Fetch recent diagnostic record
                cur.execute(
                    """
                    SELECT retrieved_signature_ids, confidence 
                    FROM diagnostic_records 
                    WHERE session_id = %s 
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,)
                )
                diag_row = cur.fetchone()
                
                # --- FIXED INDENTATION BLOCK ---
                retrieved_signature_ids = []
                confidence = 0.0
                if diag_row:
                    sig_ids_raw = diag_row[0]
                    if isinstance(sig_ids_raw, str):
                        stripped = sig_ids_raw.strip('{}')
                        retrieved_signature_ids = stripped.split(',') if stripped else []
                    else:
                        retrieved_signature_ids = sig_ids_raw or []
                    confidence = diag_row[1] or 0.0
                # -------------------------------
                
                # 4. DECIDE THE PATH
                if retrieved_signature_ids and len(retrieved_signature_ids) > 0 and confidence >= (1.0 - NOVELTY_THRESHOLD):
                    # 5a. MEMORY-INFORMED PATH
                    signature_id = retrieved_signature_ids[0]
                    cur.execute(
                        "SELECT resolved_params, outcome_score FROM drift_signatures WHERE signature_id = %s",
                        (signature_id,)
                    )
                    sig_row = cur.fetchone()
                    
                    if sig_row:
                        resolved_params = sig_row[0]
                        outcome_score = sig_row[1]
                        
                        if isinstance(resolved_params, str):
                            proposed_params = json.loads(resolved_params)
                        else:
                            proposed_params = resolved_params
                            
                        projected_recovery = outcome_score
                        calibration_method = "memory_informed"
                    else:
                        # Fallback if signature missing
                        proposed_params = {"rotation_deg": 0.0, "gain_adjustment": 1.0, "channel_reweight": False, "error": "signature_not_found"}
                        projected_recovery = 0.5
                        calibration_method = "memory_informed_failed"
                else:
                    # 5b. BASELINE PATH
                    cur.execute(
                        """
                        SELECT trials FROM telemetry_events 
                        WHERE session_id = %s AND trials IS NOT NULL 
                        ORDER BY ts DESC LIMIT 1
                        """,
                        (session_id,)
                    )
                    telemetry_row = cur.fetchone()
                    
                    if telemetry_row and telemetry_row[0]:
                        trials_data = telemetry_row[0]
                        if isinstance(trials_data, str):
                            trials_data = json.loads(trials_data)
                            
                        # Extract the actual trials list
                        trials_list = trials_data.get("trials", [])
                        
                        if trials_list:
                            estimated_drift_deg = estimate_rotation_deg(trials_list)
                            correction_deg = -estimated_drift_deg
                            
                            proposed_params = {
                                "rotation_deg": round(correction_deg, 2),
                                "gain_adjustment": 1.0,
                                "channel_reweight": False,
                                "estimated_drift_deg": round(estimated_drift_deg, 2)
                            }
                            projected_recovery = 0.75
                            calibration_method = "baseline"
                        else:
                            proposed_params = {"rotation_deg": 0.0, "gain_adjustment": 1.0, "channel_reweight": False, "error": "empty_trials_list"}
                            projected_recovery = 0.5
                            calibration_method = "baseline_no_data"
                    else:
                        proposed_params = {"rotation_deg": 0.0, "gain_adjustment": 1.0, "channel_reweight": False, "error": "no_trial_data_available"}
                        projected_recovery = 0.5
                        calibration_method = "baseline_no_data"
                
                # 6. Insert into calibration_proposals
                cur.execute(
                    """
                    INSERT INTO calibration_proposals (session_id, proposed_params, projected_recovery, calibration_method)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (session_id, json.dumps(proposed_params), projected_recovery, calibration_method)
                )
                
                # 7. Transition session forward
                cur.execute(
                    """
                    UPDATE session_state 
                    SET state = 'awaiting_approval', version = version + 1, updated_at = now() 
                    WHERE session_id = %s AND version = %s
                    """,
                    (session_id, new_version)
                )
                
                # 8. Insert into agent_audit_log
                latency_ms = int((time.time() - start_time) * 1000)
                cur.execute(
                    """
                    INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                    VALUES ('calibration_agent', 'compute_calibration', %s, 'success', %s)
                    """,
                    (session_id, latency_ms)
                )
                
                # 9. Commit per session
                conn.commit()
                proposals_written += 1
                
            except Exception as session_err:
                conn.rollback()
                logger.error("Error processing session %s: %s", session_id, str(session_err))
                continue
                
        # 10. Return summary
        return {
            "statusCode": 200,
            "body": {
                "sessions_processed": sessions_processed,
                "proposals_written": proposals_written
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