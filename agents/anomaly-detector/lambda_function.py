import psycopg2
import os
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ANGLE_ERROR_THRESHOLD = 15
KL_DIVERGENCE_THRESHOLD = 0.5

def lambda_handler(event, context):
    sessions_checked = 0
    anomalies_flagged = 0
    conflicts_skipped = 0

    conn = None
    cur = None
    
    try:
        cert_path = os.path.join(os.path.dirname(__file__), "root.crt")
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=cert_path
        )
        cur = conn.cursor()

        cur.execute(
            "SELECT session_id, patient_id, version FROM session_state WHERE state = 'monitoring'"
        )
        sessions = cur.fetchall()

        for session in sessions:
            session_id, patient_id, version = session
            sessions_checked += 1
            session_start_time = time.time()

            try:
                cur.execute(
                    "SELECT angle_error, kl_divergence FROM telemetry_events WHERE session_id = %s ORDER BY ts DESC LIMIT 1",
                    (session_id,)
                )
                telemetry = cur.fetchone()

                if not telemetry:
                    continue

                angle_error, kl_divergence = telemetry

                if angle_error > ANGLE_ERROR_THRESHOLD or kl_divergence > KL_DIVERGENCE_THRESHOLD:
                    cur.execute(
                        """
                        UPDATE session_state 
                        SET state = 'anomaly_detected', 
                            locked_by_agent = 'anomaly_detection_agent', 
                            version = version + 1, 
                            updated_at = now() 
                        WHERE session_id = %s AND version = %s
                        """,
                        (session_id, version)
                    )
                    
                    latency_ms = int((time.time() - session_start_time) * 1000)

                    if cur.rowcount == 1:
                        anomalies_flagged += 1
                        cur.execute(
                            """
                            INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                            VALUES ('anomaly_detection_agent', 'flag_anomaly', %s, 'anomaly_detected', %s)
                            """,
                            (session_id, latency_ms)
                        )
                    else:
                        conflicts_skipped += 1
                        cur.execute(
                            """
                            INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                            VALUES ('anomaly_detection_agent', 'flag_anomaly', %s, 'claim_conflict_skipped', %s)
                            """,
                            (session_id, latency_ms)
                        )
                
                conn.commit()

            except Exception as session_err:
                conn.rollback()
                logger.error("Error processing session %s: %s", session_id, str(session_err))

        return {
            "statusCode": 200,
            "body": {
                "sessions_checked": sessions_checked,
                "anomalies_flagged": anomalies_flagged,
                "conflicts_skipped": conflicts_skipped
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