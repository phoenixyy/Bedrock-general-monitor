import aws_cdk as cdk
from bedrock_monitoring.stack import BedrockMonitoringStack

app = cdk.App()

BedrockMonitoringStack(
    app,
    "BedrockMonitoringStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "us-east-1",
    ),
)

app.synth()
