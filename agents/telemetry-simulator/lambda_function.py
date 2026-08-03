import psycopg2
import os
import uuid
import random
import json
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    patient_id = event.get("patient_id")
    mode = event.get("mode", "ambient")
    
    conn = None
    try:
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
        cur = conn.cursor()
        
        cur.execute(
            "SELECT session_id, state, version FROM session_state WHERE patient_id = %s AND state NOT IN ('resolved','rejected') ORDER BY updated_at DESC LIMIT 1",
            (patient_id,)
        )
        row = cur.fetchone()
        
        if not row:
            session_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO session_state (session_id, patient_id, state) VALUES (%s, %s, 'monitoring')",
                (session_id, patient_id)
            )
        else:
            session_id = row[0]
            state = row[1]
            if state != 'monitoring':
                conn.commit()
                return {
                    "statusCode": 200,
                    "body": {
                        "session_id": session_id,
                        "mode": mode,
                        "skipped": True,
                        "reason": f"session frozen, current state is '{state}'"
                    }
                }
            if mode == 'injected':
                cur.execute(
                    "UPDATE session_state SET version = version + 1, updated_at = now() WHERE session_id = %s",
                    (session_id,)
                )
        
        start_time = time.time()
        
        if mode == "ambient":
            angle_error = round(random.uniform(2.0, 9.0), 2)
            kl_divergence = round(random.uniform(0.05, 0.2), 3)
            decoder_state = "Baseline"
        else:
            angle_error = round(random.uniform(18.0, 32.0), 2)
            kl_divergence = round(random.uniform(0.55, 0.85), 3)
            decoder_state = "Drifted"
            
        channel_metrics = json.dumps({f"channel_{i}": round(random.uniform(0.5, 1.5), 3) for i in range(1, 9)})
        
        if mode == "ambient":
            true_drift_deg = random.uniform(-2, 2)
        else:
            true_drift_deg = random.uniform(15, 35) * random.choice([1, -1])
            
        trials_list = []
        for intended in [0, 45, 90, 135, 180, 225, 270, 315]:
            decoded = (intended + true_drift_deg + random.uniform(-3, 3)) % 360
            trials_list.append({
                "intended": intended,
                "decoded": decoded
            })
            
        trials_json = json.dumps({
            "true_drift_deg": true_drift_deg,
            "trials": trials_list
        })
        
        cur.execute(
            "INSERT INTO telemetry_events (session_id, patient_id, angle_error, kl_divergence, decoder_state, channel_metrics, trials) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id, patient_id, angle_error, kl_divergence, decoder_state, channel_metrics, trials_json)
        )
        
        latency_ms = int((time.time() - start_time) * 1000)
        
        cur.execute(
            "INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) VALUES ('telemetry_simulator', 'insert_telemetry', %s, 'success', %s)",
            (session_id, latency_ms)
        )
        
        conn.commit()
        
        return {
            "statusCode": 200,
            "body": {
                "session_id": session_id,
                "mode": mode,
                "angle_error": angle_error,
                "kl_divergence": kl_divergence
            }
        }
        
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(str(e))
        return {
            "statusCode": 500,
            "body": {
                "error": str(e)
            }
        }
        
    finally:
        if conn:
            conn.close()