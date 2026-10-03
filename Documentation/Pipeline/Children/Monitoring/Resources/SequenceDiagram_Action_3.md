```mermaid
sequenceDiagram
    box Local machine
        participant Operator
        participant Terraform
        participant Monitor as Monitoring script
    end
    box AWS
        participant CW as CloudWatch
    end
    box Storage
        participant S3 as S3 storage
    end
    Note over Operator,S3: Publish the report and metrics
    Note over Monitor: Batch analysis ready to publish
    Monitor->>S3: write batch monitoring report
    Monitor->>CW: publish aggregate metrics as live or replay
    CW-->>Monitor: metrics accepted
    Monitor-->>Operator: report location and assessment summary
    Note over Operator: Monitoring results available

    Note over Operator,S3: Review monitoring results
    Note over Operator: Operator opens the monitoring dashboard
    Operator->>CW: read dashboard and live alarm states
    CW-->>Operator: live and replay charts with live alarm states
    Note over Operator: Results ready for investigation
```
