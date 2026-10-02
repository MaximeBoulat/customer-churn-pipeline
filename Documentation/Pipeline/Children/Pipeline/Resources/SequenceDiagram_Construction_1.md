```mermaid
sequenceDiagram
    box Local machine
        participant Operator
        participant Terraform
        participant Launcher as run_pipeline.py
    end
    box AWS
        participant Pipeline as SageMaker pipeline
        participant Jobs as Processing and training jobs
        participant Group as Feature Group
        participant Registry as Model package group
    end
    box Storage
        participant S3
    end
    Note over Operator,S3: Provision pipeline infrastructure
    Note over Operator: Curated Parquet, buckets and LabRole already exist
    Operator->>Terraform: apply pipeline configuration
    Terraform->>S3: upload processing Python files
    Terraform->>Group: create or update versioned Feature Group
    Group-->>Terraform: group ready
    Terraform->>Registry: create model package group
    Registry-->>Terraform: group ready
    Terraform->>Pipeline: register DAG as JSON
    Pipeline-->>Terraform: pipeline ready
    Terraform-->>Operator: apply complete
    Note over Operator: Infrastructure ready; no execution started
```
