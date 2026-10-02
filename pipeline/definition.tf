# This is the only pipeline definition. Terraform encodes it as AWS's JSON DAG.
locals {
  # Pinned implementation dependency; shared by processing, training and inference.
  xgboost_image = "683313688378.dkr.ecr.us-east-1.amazonaws.com/sagemaker-xgboost:1.7-1"

  execution_id = { Get = "Execution.PipelineExecutionId" }
  run_path = {
    "Std:Join" = {
      On     = "/"
      Values = ["s3://${var.output_bucket}/pipeline", local.execution_id]
    }
  }
  model_path = {
    "Std:Join" = {
      On     = "/"
      Values = ["s3://${var.output_bucket}/models/cell2cell/${var.model_version}", local.execution_id]
    }
  }
  contract_uri = {
    "Std:Join" = {
      On     = "/"
      Values = [local.run_path, "process/contract/preprocessing_parameters.json"]
    }
  }
  evaluation_uri = {
    "Std:Join" = {
      On     = "/"
      Values = [local.model_path, "evaluation/evaluation.json"]
    }
  }
  processing_base = {
    RoleArn = data.aws_iam_role.execution.arn
    ProcessingResources = {
      ClusterConfig = { InstanceType = var.instance_type, InstanceCount = 1, VolumeSizeInGB = 30 }
    }
    StoppingCondition = { MaxRuntimeInSeconds = 7200 }
  }
  code_input = {
    InputName = "code"
    S3Input = {
      S3Uri                  = "s3://${var.output_bucket}/${local.code_prefix}/"
      LocalPath              = "/opt/ml/processing/input/code"
      S3DataType             = "S3Prefix"
      S3InputMode            = "File"
      S3DataDistributionType = "FullyReplicated"
    }
  }
  process_outputs = [for name in ["train", "validation", "test", "contract", "identifiers"] : {
    OutputName = name
    S3Output = {
      S3Uri = { "Std:Join" = { On = "/", Values = [local.run_path, "process", name] } }
      LocalPath    = "/opt/ml/processing/output/${name}"
      S3UploadMode = "EndOfJob"
    }
  }]
  process_inputs = { for name in ["train", "validation", "test", "contract", "identifiers"] : name => {
    InputName = name
    S3Input = {
      S3Uri                  = { Get = "Steps.Preprocess.ProcessingOutputConfig.Outputs['${name}'].S3Output.S3Uri" }
      LocalPath              = "/opt/ml/processing/input/${name}"
      S3DataType             = "S3Prefix"
      S3InputMode            = "File"
      S3DataDistributionType = "FullyReplicated"
    }
  } }

  definition = {
    Version    = "2020-12-01"
    Metadata   = {}
    Parameters = []
    Steps = [
      {
        Name = "Preprocess"
        Type = "Processing"
        Arguments = merge(local.processing_base, {
          AppSpecification = {
            ImageUri            = local.xgboost_image
            ContainerEntrypoint = ["python3", "/opt/ml/processing/input/code/process.py"]
            ContainerArguments  = ["--preprocessing-version", var.preprocessing_contract, "--curated-version", var.curated_version, "--feature-version", var.feature_version]
          }
          ProcessingInputs = [local.code_input, {
            InputName = "curated"
            S3Input = {
              S3Uri                  = "s3://${var.output_bucket}/curated/cell2cell/${var.curated_version}/split=train/"
              LocalPath              = "/opt/ml/processing/input/curated"
              S3DataType             = "S3Prefix"
              S3InputMode            = "File"
              S3DataDistributionType = "FullyReplicated"
            }
          }]
          ProcessingOutputConfig = { Outputs = local.process_outputs }
        })
      },
      {
        Name = "IngestFeatures"
        Type = "Processing"
        Arguments = merge(local.processing_base, {
          AppSpecification = {
            ImageUri            = local.xgboost_image
            ContainerEntrypoint = ["python3", "/opt/ml/processing/input/code/ingest.py"]
            ContainerArguments  = ["--feature-group", aws_sagemaker_feature_group.features.feature_group_name, "--region", var.region, "--execution-id", local.execution_id]
          }
          ProcessingInputs = concat([local.code_input], [for name in ["train", "validation", "test", "contract", "identifiers"] : local.process_inputs[name]])
          ProcessingOutputConfig = { Outputs = [{
            OutputName = "ingestion"
            S3Output = {
              S3Uri        = { "Std:Join" = { On = "/", Values = [local.run_path, "feature-store"] } }
              LocalPath    = "/opt/ml/processing/output"
              S3UploadMode = "EndOfJob"
            }
          }] }
        })
      },
      {
        Name      = "Train"
        Type      = "Training"
        DependsOn = ["IngestFeatures"]
        Arguments = {
          RoleArn                = data.aws_iam_role.execution.arn
          AlgorithmSpecification = { TrainingImage = local.xgboost_image, TrainingInputMode = "File" }
          ResourceConfig         = { InstanceType = var.instance_type, InstanceCount = 1, VolumeSizeInGB = 30 }
          StoppingCondition      = { MaxRuntimeInSeconds = 3600 }
          OutputDataConfig       = { S3OutputPath = local.model_path }
          HyperParameters = {
            objective = "binary:logistic", max_depth = "5", eta = "0.2", gamma = "4",
            min_child_weight = "6", subsample = "0.8", verbosity = "0", num_round = "100",
            eval_metric = "auc", seed = "540"
          }
          InputDataConfig = [for name in ["train", "validation"] : {
            ChannelName = name
            ContentType = "text/csv"
            DataSource = { S3DataSource = {
              S3DataType             = "S3Prefix"
              S3Uri                  = local.process_inputs[name].S3Input.S3Uri
              S3DataDistributionType = "FullyReplicated"
            } }
          }]
        }
      },
      {
        Name = "Evaluate"
        Type = "Processing"
        Arguments = merge(local.processing_base, {
          AppSpecification = {
            ImageUri            = local.xgboost_image
            ContainerEntrypoint = ["python3", "/opt/ml/processing/input/code/evaluate.py"]
          }
          ProcessingInputs = [local.code_input, local.process_inputs.test, {
            InputName = "model"
            S3Input = {
              S3Uri                  = { Get = "Steps.Train.ModelArtifacts.S3ModelArtifacts" }
              LocalPath              = "/opt/ml/processing/input/model"
              S3DataType             = "S3Prefix"
              S3InputMode            = "File"
              S3DataDistributionType = "FullyReplicated"
            }
          }]
          ProcessingOutputConfig = { Outputs = [{
            OutputName = "evaluation"
            S3Output = {
              S3Uri        = { "Std:Join" = { On = "/", Values = [local.model_path, "evaluation"] } }
              LocalPath    = "/opt/ml/processing/output"
              S3UploadMode = "EndOfJob"
            }
          }] }
        })
        PropertyFiles = [{ PropertyFileName = "EvaluationReport", OutputName = "evaluation", FilePath = "evaluation.json" }]
      },
      {
        Name = "QualityGate"
        Type = "Condition"
        Arguments = {
          Conditions = [{
            Type = "GreaterThanOrEqualTo"
            LeftValue = { "Std:JsonGet" = {
              PropertyFile = { Get = "Steps.Evaluate.PropertyFiles.EvaluationReport" }
              Path         = "binary_classification_metrics.auc.value"
            } }
            RightValue = var.auc_threshold
          }]
          IfSteps = [{
            Name = "RegisterModel"
            Type = "RegisterModel"
            Arguments = {
              ModelPackageGroupName = aws_sagemaker_model_package_group.models.model_package_group_name
              ModelApprovalStatus   = "PendingManualApproval"
              ModelMetrics = { ModelQuality = { Statistics = {
                ContentType = "application/json"
                S3Uri       = local.evaluation_uri
              } } }
              CustomerMetadataProperties = {
                preprocessing_uri     = local.contract_uri
                curated_version       = var.curated_version
                preprocessing_version = var.preprocessing_contract
                feature_version       = var.feature_version
                model_version         = var.model_version
              }
              InferenceSpecification = {
                Containers = [{ Image = local.xgboost_image, ModelDataUrl = { Get = "Steps.Train.ModelArtifacts.S3ModelArtifacts" } }]
                SupportedContentTypes              = ["text/csv"]
                SupportedResponseMIMETypes          = ["text/csv"]
                SupportedTransformInstanceTypes    = [var.instance_type]
                SupportedRealtimeInferenceInstanceTypes = [var.instance_type]
              }
            }
          }]
          ElseSteps = [{
            Name      = "QualityGateFailed"
            Type      = "Fail"
            Arguments = { ErrorMessage = "Test AUC below the configured minimum; model not registered." }
          }]
        }
      }
    ]
  }
}
