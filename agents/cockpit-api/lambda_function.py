import psycopg2
import os
import json
import time
import logging
import datetime
import uuid

# Configure basic logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# CORS Headers for API Gateway HTTP API
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
}

class CustomJSONEncoder(json.JSONEncoder):
    """Handles serialization of UUIDs and Datetimes to strings."""
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return super().default(obj)

def build_response(status_code, body_data):
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body_data, cls=CustomJSONEncoder)
    }

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    raw_path = event.get("rawPath", "")
    
    # 1. Handle CORS Preflight immediately
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
        
    conn = None
    cur = None
    
    try:
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
        cur = conn.cursor()
        
        # ROUTE 1: GET /sessions
        if method == "GET" and raw_path == "/sessions":
            cur.execute("""
                SELECT ss.session_id, ss.patient_id, p.device_id, 
                       dr.hypothesis, dr.confidence, cp.proposed_params, cp.calibration_method 
                FROM session_state ss 
                LEFT JOIN patients p ON ss.patient_id = p.patient_id 
                LEFT JOIN LATERAL (
                    SELECT hypothesis, confidence FROM diagnostic_records 
                    WHERE session_id = ss.session_id ORDER BY created_at DESC LIMIT 1
                ) dr ON true 
                LEFT JOIN LATERAL (
                    SELECT proposed_params, calibration_method FROM calibration_proposals 
                    WHERE session_id = ss.session_id ORDER BY created_at DESC LIMIT 1
                ) cp ON true 
                WHERE ss.state = 'awaiting_approval'
            """)
            
            rows = cur.fetchall()
            sessions_list = []
            for row in rows:
                params = row[5]
                if isinstance(params, str):
                    try:
                        params = json.loads(params)
                    except json.JSONDecodeError:
                        pass
                        
                sessions_list.append({
                    "session_id": row[0],
                    "patient_id": row[1],
                    "device_id": row[2],
                    "hypothesis": row[3],
                    "confidence": row[4],
                    "proposed_params": params,
                    "calibration_method": row[6]
                })
            return build_response(200, sessions_list)
            
        # ROUTE 2: GET /sessions/{session_id}
        elif method == "GET" and raw_path.startswith("/sessions/") and "session_id" in event.get("pathParameters", {}):
            session_id = event["pathParameters"]["session_id"]
            
            # Fetch session state
            cur.execute("SELECT state, patient_id FROM session_state WHERE session_id = %s", (session_id,))
            session_row = cur.fetchone()
            if not session_row:
                return build_response(404, {"error": "session not found"})
                
            # Fetch diagnosis
            cur.execute("""
                SELECT hypothesis, confidence, retrieved_signature_ids 
                FROM diagnostic_records 
                WHERE session_id = %s ORDER BY created_at DESC LIMIT 1
            """, (session_id,))
            diag_row = cur.fetchone()
            
            hypothesis, confidence, sig_ids = None, None, []
            if diag_row:
                hypothesis, confidence, sig_ids = diag_row
                
            # Fetch cited memories
            cited_memories = []
            if sig_ids and len(sig_ids) > 0:
                cur.execute("""
                    SELECT signature_id, root_cause_label, feature_summary, outcome_score 
                    FROM drift_signatures 
                    WHERE signature_id = ANY(%s)
                """, (sig_ids,))
                for sig in cur.fetchall():
                    cited_memories.append({
                        "signature_id": sig[0],
                        "root_cause_label": sig[1],
                        "feature_summary": sig[2] if not isinstance(sig[2], str) else json.loads(sig[2]),
                        "outcome_score": sig[3]
                    })
                    
            # Fetch calibration proposal
            cur.execute("""
                SELECT proposed_params, calibration_method, projected_recovery 
                FROM calibration_proposals 
                WHERE session_id = %s ORDER BY created_at DESC LIMIT 1
            """, (session_id,))
            cal_row = cur.fetchone()
            proposed_params, calibration_method, projected_recovery = None, None, None
            if cal_row:
                proposed_params = cal_row[0] if not isinstance(cal_row[0], str) else json.loads(cal_row[0])
                calibration_method = cal_row[1]
                projected_recovery = cal_row[2]
                
            # Fetch telemetry history (last 10)
            cur.execute("""
    SELECT ts, angle_error, kl_divergence FROM (
        SELECT ts, angle_error, kl_divergence FROM telemetry_events 
        WHERE session_id = %s ORDER BY ts DESC LIMIT 10
       ) sub ORDER BY ts ASC
    """, (session_id,))
            telemetry_history = []
            for tel in cur.fetchall():
                telemetry_history.append({
                    "ts": tel[0],
                    "angle_error": tel[1],
                    "kl_divergence": tel[2]
                })
                
            response_data = {
                "session": {"state": session_row[0], "patient_id": session_row[1]},
                "diagnosis": {"hypothesis": hypothesis, "confidence": confidence},
                "cited_memories": cited_memories,
                "calibration": {
                    "proposed_params": proposed_params,
                    "calibration_method": calibration_method,
                    "projected_recovery": projected_recovery
                },
                "telemetry_history": telemetry_history
            }
            return build_response(200, response_data)
            
        # ROUTE 3: POST /sessions/{session_id}/decide
        elif method == "POST" and raw_path.endswith("/decide") and "session_id" in event.get("pathParameters", {}):
            session_id = event["pathParameters"]["session_id"]
            start_time = time.time()
            
            # Parse Body
            try:
                body = json.loads(event.get("body", "{}"))
            except json.JSONDecodeError:
                return build_response(400, {"error": "Invalid JSON body"})
                
            operator_id = body.get("operator_id")
            decision = body.get("decision")
            reason = body.get("reason")
            final_params = body.get("final_params")
            
            if not operator_id or decision not in ["approve", "reject"]:
                return build_response(400, {"error": "Missing operator_id or invalid decision"})
                
            # Fetch current session state
            cur.execute("SELECT session_id, state, version FROM session_state WHERE session_id = %s", (session_id,))
            state_row = cur.fetchone()
            
            if not state_row:
                return build_response(404, {"error": "session not found"})
                
            current_state = state_row[1]
            version = state_row[2]
            
            if current_state != 'awaiting_approval':
                return build_response(409, {"error": f"session is not awaiting approval, current state is {current_state}"})
                
            # Insert Approval Event
            params_to_save = json.dumps(final_params) if decision == "approve" and final_params else None
            cur.execute("""
                INSERT INTO approval_events (session_id, operator_id, decision, final_params, reason) 
                VALUES (%s, %s, %s, %s, %s)
            """, (session_id, operator_id, decision, params_to_save, reason))
            
            # State Transition with OCC
            new_state = 'deploying' if decision == 'approve' else 'rejected'
            cur.execute("""
                UPDATE session_state 
                SET state = %s, version = version + 1, updated_at = now() 
                WHERE session_id = %s AND version = %s
            """, (new_state, session_id, version))
            
            if cur.rowcount == 0:
                conn.rollback()
                return build_response(409, {"error": "session state changed concurrently, please refresh"})
                
            # Audit Log
            latency_ms = int((time.time() - start_time) * 1000)
            cur.execute("""
                INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                VALUES ('cockpit_backend', 'record_decision', %s, 'success', %s)
            """, (session_id, latency_ms))
            
            conn.commit()
            return build_response(200, {"session_id": session_id, "new_state": new_state})
            
        else:
            return build_response(404, {"error": "Route not found"})
            
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"API Error: {str(e)}")
        return build_response(500, {"error": "Internal server error"})
        
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
