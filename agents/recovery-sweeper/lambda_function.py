import psycopg2
import os
import time
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Constants
STUCK_CLAIM_MINUTES = 5
STALE_RESTING_MINUTES = 15

def lambda_handler(event, context):
    stuck_sessions_recovered = 0
    stale_sessions_flagged = 0

    # Category 1: Transient claimed states that should self-heal
    category_1_transitions = {
        'diagnosing': 'anomaly_detected',
        'computing_calibration': 'calibrating',
        'executing_deployment': 'deploying'
    }

    # Category 2: Resting states awaiting a scheduled agent
    category_2_states = [
        'anomaly_detected',
        'calibrating',
        'deploying'
    ]

    conn = None
    try:
        # Establish database connection
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
        
        with conn.cursor() as cur:
            # CATEGORY 1: Reset stuck claimed sessions
            for current_state, prior_resting_state in category_1_transitions.items():
                # Parameterize the state name and the interval properly for psycopg2
                select_query = """
                    SELECT session_id, version 
                    FROM session_state 
                    WHERE state = %s 
                    AND updated_at < now() - %s::interval
                """
                cur.execute(select_query, (current_state, f"{STUCK_CLAIM_MINUTES} minutes"))
                stuck_sessions = cur.fetchall()
                
                for session_id, version in stuck_sessions:
                    update_query = """
                        UPDATE session_state 
                        SET state = %s, locked_by_agent = NULL, version = version + 1, updated_at = now() 
                        WHERE session_id = %s AND state = %s
                    """
                    cur.execute(update_query, (prior_resting_state, session_id, current_state))
                    
                    if cur.rowcount == 1:
                        insert_audit_query = """
                            INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                            VALUES (%s, %s, %s, %s, %s)
                        """
                        cur.execute(insert_audit_query, (
                            'recovery_sweeper',
                            'reset_stuck_session',
                            session_id,
                            'recovered_stuck_session',
                            0
                        ))
                        stuck_sessions_recovered += 1

            # CATEGORY 2: Log stale resting states
            for resting_state in category_2_states:
                select_query = """
                    SELECT session_id, version 
                    FROM session_state 
                    WHERE state = %s 
                    AND updated_at < now() - %s::interval
                """
                cur.execute(select_query, (resting_state, f"{STALE_RESTING_MINUTES} minutes"))
                stale_sessions = cur.fetchall()
                
                for session_id, version in stale_sessions:
                    insert_audit_query = """
                        INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms) 
                        VALUES (%s, %s, %s, %s, %s)
                    """
                    cur.execute(insert_audit_query, (
                        'recovery_sweeper',
                        'stale_resting_check',
                        session_id,
                        'stale_resting_state',
                        0
                    ))
                    stale_sessions_flagged += 1
            
            # Commit the transaction
            conn.commit()

    except Exception as e:
        logger.error(f"Error during recovery sweep: {str(e)}")
        if conn is not None:
            conn.rollback()
        
        return {
            "statusCode": 500,
            "body": {
                "error": "Failed to complete recovery sweep",
                "details": str(e)
            }
        }
    finally:
        if conn is not None:
            conn.close()

    return {
        "statusCode": 200,
        "body": {
            "stuck_sessions_recovered": stuck_sessions_recovered,
            "stale_sessions_flagged": stale_sessions_flagged
        }
    }