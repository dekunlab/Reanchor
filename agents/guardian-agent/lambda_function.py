import psycopg2
import os
import json
import time
import logging
import urllib.request
import urllib.error

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    start_time = time.time()
    
    # 1. Call the Cloud API endpoint
    cluster_id = os.environ.get("CC_CLUSTER_ID")
    api_key = os.environ.get("CC_API_KEY")
    url = f"https://cockroachlabs.cloud/api/v1/clusters/{cluster_id}"
    
    cluster_status_summary = {}
    
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"}
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            response_body = response.read().decode('utf-8')
            response_dict = json.loads(response_body)
            
            # Log raw response for CloudWatch inspectability
            logger.info(f"Raw CockroachDB Cloud API response: {json.dumps(response_dict)}")
            
            cluster_status_summary = {
                "reachable": True,
                "raw_state": response_dict.get("state", response_dict)
            }
            
    except Exception as e:
        logger.error(f"Failed to fetch cluster status: {str(e)}")
        cluster_status_summary = {
            "reachable": False,
            "error": str(e)
        }

    conn = None
    try:
        # Establish database connection
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            sslrootcert=os.path.join(os.path.dirname(__file__), "root.crt")
        )
        
        with conn.cursor() as cur:
            # 2. Query internal audit log for a 24-hour summary
            query = """
                SELECT agent_name, status, count(*) 
                FROM agent_audit_log 
                WHERE ts > now() - INTERVAL '24 hours' 
                GROUP BY agent_name, status
            """
            cur.execute(query)
            rows = cur.fetchall()
            
            audit_summary = []
            for row in rows:
                audit_summary.append({
                    "agent_name": row[0],
                    "status": row[1],
                    "count": row[2]
                })
            
            # 3. Determine overall digest_status
            has_error_status = any(item.get("status") == "error" for item in audit_summary)
            
            if cluster_status_summary.get("reachable", False) and not has_error_status:
                digest_status = "healthy"
            else:
                digest_status = "attention_needed"
                
            # 4. Log the full digest at INFO level as a single JSON blob
            digest_log = {
                "cluster_status": cluster_status_summary,
                "audit_summary": audit_summary,
                "digest_status": digest_status
            }
            logger.info(f"Compliance Digest: {json.dumps(digest_log)}")
            
            # 5. Insert one row into agent_audit_log
            latency_ms = int((time.time() - start_time) * 1000)
            
            insert_query = """
                INSERT INTO agent_audit_log (agent_name, tool_called, session_id, status, latency_ms)
                VALUES (%s, %s, NULL, %s, %s)
            """
            cur.execute(insert_query, (
                "guardian_agent",
                "compliance_digest",
                digest_status,
                latency_ms
            ))
            
            # 6. Commit transaction
            conn.commit()
            
    except Exception as e:
        logger.error(f"Database operation failed during compliance digest generation: {str(e)}")
        if conn is not None:
            conn.rollback()
        raise e
        
    finally:
        # 6 (cont). Close the connection in a finally block
        if conn is not None:
            conn.close()

    # 7. Return expected payload
    return {
        "statusCode": 200,
        "body": {
            "digest_status": digest_status,
            "cluster_reachable": cluster_status_summary.get("reachable", False),
            "audit_summary": audit_summary
        }
    }