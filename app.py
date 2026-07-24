import aws_cdk as cdk
from bedrock_monitoring.stack import BedrockMonitoringStack
from bedrock_monitoring.devops_agent_stack import DevOpsAgentStack

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

# 可选：AWS DevOps Agent Space（cdk.json 中 deploy_devops_agent=true 时才部署）
# 独立 Stack，不影响 BedrockMonitoringStack；deploy_devops_agent=false（默认）时不合成，
# 未开通 DevOps Agent 服务或所在 Region 不支持时不会因此报错。
if app.node.try_get_context("deploy_devops_agent"):
    DevOpsAgentStack(
        app,
        "DevOpsAgentStack",
        agent_space_name=app.node.try_get_context("devops_agent_space_name")
        or "BedrockMonitoringAgentSpace",
        env=env,
    )

app.synth()
