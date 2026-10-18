"""Record SageMaker-native stages that cannot update our manifest themselves."""
import argparse

import manifest


def training(execution_id, job_name, model_data):
    manifest.update_manifest(execution_id, "training",
                             xgboost={"training_job_name": job_name, "model_data": model_data})


def outcome(execution_id, gate, package_arn=None):
    if (gate == "passed") != bool(package_arn):
        raise ValueError("A passed gate requires a registered package; a failed gate must have none")
    manifest.update_manifest(execution_id, "gate",
                             status="completed" if gate == "passed" else "failed")
    if package_arn:
        manifest.update_manifest(execution_id, "registration",
                                 model_package_arn=package_arn,
                                 model_package_version=int(package_arn.rsplit("/", 1)[-1]))
    else:
        manifest.update_manifest(execution_id, "registration", status="skipped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--gate", choices=("passed", "failed"), required=True)
    parser.add_argument("--model-package-arn")
    args = parser.parse_args()
    outcome(args.execution_id, args.gate, args.model_package_arn)


if __name__ == "__main__":
    main()
