"""
可选 Stack：部署 AWS DevOps Agent 的 Agent Space（监控账号侧）。

只在 cdk.json context 中 deploy_devops_agent=true 时被 app.py 实例化。
资源结构对齐 AWS 官方 CloudFormation 模板（getting-started-with-aws-devops-agent-using-aws-cloudformation）：
- IAM Role: DevOpsAgentRole-AgentSpace（DevOps Agent 服务用来监控账号）
- IAM Role: DevOpsAgentRole-WebappAdmin（Operator App 角色）
- AWS::DevOpsAgent::AgentSpace（Agent Space 本体）
- AWS::DevOpsAgent::Association（AccountType=monitor，把当前账号关联进 Agent Space）

AWS::DevOpsAgent::* 是较新的 CFN 资源类型，aws-cdk-lib 目前没有对应的 L1 构造类，
用 CfnResource 逃生舱写，等官方发布 L1 后可以直接替换成 aws_cdk.aws_devopsagent.CfnAgentSpace 等。
"""

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct


class DevOpsAgentStack(cdk.Stack):
    """
    仅创建监控账号（Part 1）资源。跨账号监控（Part 2，service account 的
    DevOpsAgentRole-SecondaryAccount + source association）暂不在一键部署范围内，
    需要跨账号场景时按官方文档 Part 2 手动补充，避免一键部署时误建跨账号信任关系。
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        agent_space_name: str = "BedrockMonitoringAgentSpace",
        agent_space_description: str = "Agent Space for Bedrock monitoring stack (auto-provisioned)",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # IAM Role：DevOps Agent 服务本身用来监控账号资源
        agent_space_role = iam.Role(
            self,
            "AgentSpaceRole",
            role_name="DevOpsAgentRole-AgentSpace",
            assumed_by=iam.ServicePrincipal(
                "aidevops.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:aidevops:{self.region}:{self.account}:agentspace/*"
                    },
                },
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AIDevOpsAgentAccessPolicy"
                )
            ],
        )
        agent_space_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowCreateServiceLinkedRoles",
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/aws-service-role/"
                    "resource-explorer-2.amazonaws.com/AWSServiceRoleForResourceExplorer"
                ],
            )
        )

        # IAM Role：Operator App（DevOps Agent 控制台交互界面）用的角色
        # ⚠️ trust policy 必须同时允许 sts:AssumeRole 和 sts:TagSession！
        # DevOps Agent 服务 assume 这个角色发放临时凭证时，靠 TagSession 把 AgentSpaceId 打成会话标签（Principal Tag），
        # AIDevOpsOperatorAppAccessPolicy 里那条 Resource: agentspace/${aws:PrincipalTag/AgentSpaceId} 全靠这个标签来匹配。
        # 若只有 AssumeRole 没有 TagSession，标签打不上，会导致“Launch via IAM”时 GetAgentSpace 永远 AccessDenied
        # （实测踩坑：iam.ServicePrincipal 默认只会生成 sts:AssumeRole，需手动补一条 TagSession 的 trust statement）。
        operator_role = iam.Role(
            self,
            "OperatorRole",
            role_name="DevOpsAgentRole-WebappAdmin",
            assumed_by=iam.ServicePrincipal(
                "aidevops.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:aidevops:{self.region}:{self.account}:agentspace/*"
                    },
                },
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AIDevOpsOperatorAppAccessPolicy"
                )
            ],
        )
        operator_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("aidevops.amazonaws.com")],
                actions=["sts:TagSession"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:aidevops:{self.region}:{self.account}:agentspace/*"
                    },
                },
            )
        )

        # Agent Space 本体（AWS::DevOpsAgent::AgentSpace，L1 尚未发布，用 CfnResource 逃生舱）
        agent_space = cdk.CfnResource(
            self,
            "AgentSpace",
            type="AWS::DevOpsAgent::AgentSpace",
            properties={
                "Name": agent_space_name,
                "Description": agent_space_description,
                "OperatorApp": {"Iam": {"OperatorAppRoleArn": operator_role.role_arn}},
            },
        )
        agent_space.node.add_dependency(agent_space_role)
        agent_space.node.add_dependency(operator_role)

        # Association：把当前监控账号关联进 Agent Space（AccountType=monitor）
        monitor_association = cdk.CfnResource(
            self,
            "MonitorAssociation",
            type="AWS::DevOpsAgent::Association",
            properties={
                "AgentSpaceId": agent_space.get_att("AgentSpaceId").to_string(),
                "ServiceId": "aws",
                "Configuration": {
                    "Aws": {
                        "AssumableRoleArn": agent_space_role.role_arn,
                        "AccountId": self.account,
                        "AccountType": "monitor",
                    }
                },
            },
        )
        monitor_association.node.add_dependency(agent_space)

        cdk.CfnOutput(
            self,
            "AgentSpaceIdOutput",
            key="AgentSpaceId",
            value=agent_space.get_att("AgentSpaceId").to_string(),
        )
        cdk.CfnOutput(
            self,
            "AgentSpaceArnOutput",
            key="AgentSpaceArn",
            value=agent_space.get_att("Arn").to_string(),
        )
        cdk.CfnOutput(
            self,
            "AgentSpaceRoleArnOutput",
            key="AgentSpaceRoleArn",
            value=agent_space_role.role_arn,
        )
        cdk.CfnOutput(
            self,
            "OperatorRoleArnOutput",
            key="OperatorRoleArn",
            value=operator_role.role_arn,
        )
