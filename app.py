import aws_cdk as cdk
from bedrock_monitoring.stack import BedrockMonitoringStack
from bedrock_monitoring.mantle_stack import BedrockMantleMonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-east-1",
)

BedrockMonitoringStack(
    app,
    "BedrockMonitoringStack",
    env=env,
)

# BedrockMantle 监控（openai-compatible 端点，如 GPT-5.5）为独立 Stack，
# 通过 mantle_model_ids 是否配置来决定是否部署，不影响原有 runtime 监控。
if app.node.try_get_context("mantle_model_ids"):
    BedrockMantleMonitoringStack(
        app,
        "BedrockMantleMonitoringStack",
        env=env,
    )

app.synth()
