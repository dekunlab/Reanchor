import os
import json
import random
import uuid
from datetime import datetime, timedelta

import numpy as np
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

# Five plausible root causes, each with its own "center" in 16-dim feature space.
# Signatures generated near the same center will naturally cluster together,
# so vector search recalls genuinely similar past incidents — not random noise.
ROOT_CAUSES = {
    "electrode_micro_movement": {
        "center": np.array([0.8,0.6,0.3,0.7,0.2,0.5,0.9,0.1,0.4,0.6,0.3,0.8,0.2,0.5,0.7,0.4]),
        "resolved_params": {"rotation_deg": 4.2, "gain_adjustment": 1.08, "channel_reweight": True},
    },
    "tissue_encapsulation": {
        "center": np.array([0.2,0.9,0.8,0.3,0.6,0.2,0.4,0.7,0.9,0.1,0.5,0.3,0.8,0.6,0.2,0.9]),
        "resolved_params": {"rotation_deg": 1.1, "gain_adjustment": 1.35, "channel_reweight": True},
    },
    "reference_channel_drift": {
        "center": np.array([0.5,0.2,0.6,0.9,0.4,0.8,0.1,0.5,0.3,0.7,0.9,0.2,0.6,0.4,0.8,0.1]),
        "resolved_params": {"rotation_deg": 0.4, "gain_adjustment": 1.02, "reference_reset": True},
    },
    "impedance_increase": {
        "center": np.array([0.9,0.4,0.2,0.6,0.8,0.3,0.5,0.9,0.1,0.4,0.7,0.6,0.3,0.9,0.5,0.2]),
        "resolved_params": {"rotation_deg": 2.7, "gain_adjustment": 1.5, "channel_reweight": False},
    },
    "physiological_adaptation": {
        "center": np.array([0.3,0.7,0.5,0.2,0.9,0.6,0.3,0.4,0.8,0.5,0.2,0.7,0.9,0.3,0.4,0.6]),
        "resolved_params": {"rotation_deg": 6.5, "gain_adjustment": 1.15, "channel_reweight": True},
    },
}

NUM_PATIENTS = 5
SIGNATURES_PER_PATIENT = 40  # 5 x 40 = 200 total seeded memories


def random_embedding(center, noise_scale=0.08):
    vec = center + np.random.normal(0, noise_scale, size=16)
    return np.clip(vec, 0, 1).tolist()


def to_vector_literal(vec):
    # CockroachDB's VECTOR text format: square brackets, comma-separated
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def random_past_timestamp():
    days_ago = random.randint(1, 180)
    return datetime.utcnow() - timedelta(days=days_ago, hours=random.randint(0, 23))


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    
    cur.execute("SELECT count(*) FROM drift_signatures;")
    if cur.fetchone()[0] > 0:
        print("drift_signatures already has rows — stopping to avoid duplicates.")
        cur.close(); conn.close()
        return

    patient_ids = []
    for i in range(NUM_PATIENTS):
        pid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO patients (patient_id, device_id, decoder_version) VALUES (%s, %s, %s);",
            (pid, f"sim-device-{i+1:03d}", "v1.0-sim"),
        )
        patient_ids.append(pid)

    total = 0
    for pid in patient_ids:
        for _ in range(SIGNATURES_PER_PATIENT):
            cause_name = random.choice(list(ROOT_CAUSES.keys()))
            cause = ROOT_CAUSES[cause_name]
            embedding = random_embedding(cause["center"])
            feature_summary = {
                "mean_angle_error": round(random.uniform(5, 25), 2),
                "mean_kl_divergence": round(random.uniform(0.1, 0.9), 3),
                "trend": random.choice(["increasing", "stable_high"]),
            }
            cur.execute(
                """
                INSERT INTO drift_signatures
                    (signature_id, patient_id, embedding, feature_summary,
                     root_cause_label, resolved_params, outcome_score, created_at)
                VALUES (%s, %s, %s::VECTOR, %s, %s, %s, %s, %s);
                """,
                (
                    str(uuid.uuid4()), pid, to_vector_literal(embedding),
                    json.dumps(feature_summary), cause_name,
                    json.dumps(cause["resolved_params"]),
                    round(random.uniform(0.72, 0.98), 3),
                    random_past_timestamp(),
                ),
            )
            total += 1

    conn.commit()
    cur.close(); conn.close()
    print(f"Seeded {NUM_PATIENTS} patients and {total} drift_signatures rows.")


if __name__ == "__main__":
    main()