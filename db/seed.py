import os
import random
import json
import statistics
from datetime import datetime, timedelta
from dotenv import load_dotenv
import psycopg2

load_dotenv()

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

def seed_database():
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()

        cur.execute("SELECT patient_id FROM patients")
        patients = [row[0] for row in cur.fetchall()]
        if not patients:
            raise ValueError("No patients found. Populate the patients table first.")

        profiles = {
            "electrode_micro_movement": {
                "angle": (18.0, 26.0), "kl": (0.2, 0.35),
                "c1_2": (1.3, 1.8), "c3_8": (0.9, 1.1),
                "params": {"rotation_deg": 4.2, "gain_adjustment": 1.08, "channel_reweight": True}
            },
            "tissue_encapsulation": {
                "angle": (8.0, 15.0), "kl": (0.5, 0.75),
                "c_all": (1.15, 1.35),
                "params": {"rotation_deg": 1.1, "gain_adjustment": 1.35, "channel_reweight": True}
            },
            "reference_channel_drift": {
                "angle": (5.0, 12.0), "kl": (0.3, 0.45),
                "c5": (1.5, 2.0), "c_other": (0.9, 1.1),
                "params": {"rotation_deg": 0.4, "gain_adjustment": 1.02, "reference_reset": True}
            },
            "impedance_increase": {
                "angle": (22.0, 32.0), "kl": (0.5, 0.7),
                "c346": (1.3, 1.6), "c_other": (0.9, 1.1),
                "params": {"rotation_deg": 2.7, "gain_adjustment": 1.5, "channel_reweight": False}
            },
            "physiological_adaptation": {
                "angle": (14.0, 20.0), "kl": (0.15, 0.3),
                "c_all": (1.05, 1.2),
                "params": {"rotation_deg": 6.5, "gain_adjustment": 1.15, "channel_reweight": True}
            }
        }

        insert_query = """
            INSERT INTO drift_signatures (
                patient_id, embedding, feature_summary,
                root_cause_label, resolved_params, outcome_score, created_at
            ) VALUES (%s, %s::VECTOR, %s, %s, %s, %s, %s)
        """

        summary_counts = {label: 0 for label in profiles.keys()}
        now = datetime.now()

        for label, config in profiles.items():
            for _ in range(40):
                window = []
                for _ in range(5):
                    reading = {
                        'angle_error': random.uniform(*config["angle"]),
                        'kl_divergence': random.uniform(*config["kl"]),
                        'channel_metrics': {}
                    }
                    for i in range(1, 9):
                        if label == "electrode_micro_movement":
                            val = random.uniform(*config["c1_2"]) if i in (1, 2) else random.uniform(*config["c3_8"])
                        elif label in ["tissue_encapsulation", "physiological_adaptation"]:
                            val = random.uniform(*config["c_all"])
                        elif label == "reference_channel_drift":
                            val = random.uniform(*config["c5"]) if i == 5 else random.uniform(*config["c_other"])
                        elif label == "impedance_increase":
                            val = random.uniform(*config["c346"]) if i in (3, 4, 6) else random.uniform(*config["c_other"])
                        reading['channel_metrics'][f'channel_{i}'] = val
                    window.append(reading)

                embedding_vector = build_feature_vector(window)
                embedding_str = "[" + ",".join(str(x) for x in embedding_vector) + "]"

                feature_summary = json.dumps({
                    "mean_angle_error": statistics.mean([r['angle_error'] for r in window]),
                    "mean_kl_divergence": statistics.mean([r['kl_divergence'] for r in window])
                })

                patient_id = random.choice(patients)
                outcome_score = random.uniform(0.72, 0.98)
                days_ago = random.uniform(0, 180)
                created_at = now - timedelta(days=days_ago)

                cur.execute(insert_query, (
                    patient_id, embedding_str, feature_summary,
                    label, json.dumps(config["params"]), outcome_score, created_at
                ))
                summary_counts[label] += 1

        conn.commit()
        print("Data seeding completed successfully!")
        print("-" * 40)
        for cause, count in summary_counts.items():
            print(f" * {cause}: {count} rows")
        print("-" * 40)
        print(f"Total rows inserted: {sum(summary_counts.values())}")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Database operation failed. Transaction rolled back.\nError: {str(e)}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    seed_database()