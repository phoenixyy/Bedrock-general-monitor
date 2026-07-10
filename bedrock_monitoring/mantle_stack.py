"""BedrockMantle 监控 Stack（独立于 AWS/Bedrock runtime 监控）。

覆盖 `bedrock-mantle` 端点（Responses API / Chat Completions API / Anthropic
Messages API，例如 GPT-5.5 走的 openai.gpt-5.5）的 CloudWatch 指标，
namespace 为 AWS/BedrockMantle，与 AWS/Bedrock 完全独立。

已知限制（截至文档编写时 AWS 官方状态）：
- 暂无 InvocationLatency / TimeToFirstToken 等效指标（未发布）
- 暂无 ServerErrors 指标（仅有 InferenceClientErrors）
- 暂无 Throttles 指标
"""
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)
from constructs import Construct

NAMESPACE = "AWS/BedrockMantle"


class BedrockMantleMonitoringStack(Stack):
    """独立 Stack：监控 bedrock-mantle 端点（如 GPT-5.5 / openai.gpt-5.5）。

    与 BedrockMonitoringStack（AWS/Bedrock runtime 监控）完全解耦：
    - 不同 namespace（AWS/BedrockMantle vs AWS/Bedrock）
    - 独立的 SNS Topic（bedrock-mantle-monitoring-alerts）
    - 独立部署，互不影响
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── 读取 context 参数 ────────────────────────────────────────────
        notification_email: str = (
            self.node.try_get_context("mantle_notification_email")
            or self.node.try_get_context("notification_email")
            or ""
        )
        model_ids: list = self.node.try_get_context("mantle_model_ids") or []
        project_ids: list = self.node.try_get_context("mantle_project_ids") or []
        dashboard_name: str = (
            self.node.try_get_context("mantle_dashboard_name")
            or "Bedrock-Mantle-Operations"
        )
        client_error_threshold: int = int(
            self.node.try_get_context("mantle_client_error_threshold") or 50
        )

        if not model_ids:
            raise ValueError(
                "mantle_model_ids 未配置。请在 cdk.json 的 context 中填写要监控的 "
                "bedrock-mantle 模型 ID 列表（例如 [\"openai.gpt-5.5\"]）。\n"
                "提示：可通过 `aws cloudwatch list-metrics --namespace "
                "AWS/BedrockMantle` 确认账号下实际出现的 Model 维度值。"
            )

        # ─── SNS Topic（独立于 runtime 监控的 Topic）───────────────────────
        alarm_topic = sns.Topic(
            self,
            "BedrockMantleAlarmTopic",
            topic_name="bedrock-mantle-monitoring-alerts",
            display_name="Bedrock Mantle Monitoring Alerts",
        )
        if notification_email:
            alarm_topic.add_subscription(subscriptions.EmailSubscription(notification_email))
        alarm_action = cw_actions.SnsAction(alarm_topic)

        # ─── 告警：按模型循环（Model 维度，账户级）─────────────────────────
        # BedrockMantle 目前只发布 Inferences / InferenceClientErrors 两类指标，
        # 没有 ServerErrors、Throttles、Latency/TTFT，因此能配的告警比 runtime 少。
        all_alarms: list[cw.Alarm] = []

        def _metric(metric_name: str, dims: dict, statistic: str, period_min: int, label: str = None) -> cw.Metric:
            return cw.Metric(
                namespace=NAMESPACE,
                metric_name=metric_name,
                dimensions_map=dims,
                statistic=statistic,
                period=Duration.minutes(period_min),
                label=label,
            )

        for model_id in model_ids:
            safe = model_id.replace(".", "-").replace(":", "-").replace("/", "-")
            dims = {"Model": model_id}

            # P1: InferenceClientErrors 粻增（4xx 错误，参数/权限/格式问题）
            client_err_alarm = cw.Alarm(
                self,
                f"MantleClientErrorsAlarm-{safe}",
                alarm_name=f"BedrockMantle-InferenceClientErrors-{safe}",
                alarm_description=(
                    f"[P1] {model_id} 在 bedrock-mantle 端点的 4xx 错误粻增 "
                    f"> {client_error_threshold}，检查请求参数/权限/格式"
                ),
                metric=_metric("InferenceClientErrors", dims, "Sum", 5),
                threshold=client_error_threshold,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            client_err_alarm.add_alarm_action(alarm_action)
            all_alarms.append(client_err_alarm)

            # P2: Inferences 调用量陷零（检测调用中断，例如路由/配置错导致流量归零）
            # 默认关闭，因为需要先有正常调用基线才能判断“异常跌零”，避免误报。
            if self.node.try_get_context("mantle_enable_zero_traffic_alarm") is True:
                zero_traffic_alarm = cw.Alarm(
                    self,
                    f"MantleZeroTrafficAlarm-{safe}",
                    alarm_name=f"BedrockMantle-Inferences-Zero-{safe}",
                    alarm_description=(
                        f"[P2] {model_id} 连续 15 分钟无 bedrock-mantle 调用，"
                        f"可能是路由/配置异常（需确认业务确实应有调用时才开启）"
                    ),
                    metric=_metric("Inferences", dims, "Sum", 5),
                    threshold=0,
                    comparison_operator=cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
                    evaluation_periods=3,
                    treat_missing_data=cw.TreatMissingData.BREACHING,
                )
                zero_traffic_alarm.add_alarm_action(alarm_action)
                all_alarms.append(zero_traffic_alarm)

        # ─── CloudWatch Dashboard ───────────────────────────────────────────────
        dashboard = cw.Dashboard(self, "BedrockMantleDashboard", dashboard_name=dashboard_name)

        if all_alarms:
            dashboard.add_widgets(
                cw.AlarmStatusWidget(
                    title="📋 BedrockMantle 告警状态汇总",
                    alarms=all_alarms,
                    width=24,
                    height=3,
                )
            )

        inference_metrics, client_error_metrics = [], []
        total_input_metrics, total_output_metrics = [], []

        for model_id in model_ids:
            suffix = f" · {model_id.split('.')[-1][:24]}" if len(model_ids) > 1 else ""
            dims = {"Model": model_id}

            inference_metrics.append(
                _metric("Inferences", dims, "Sum", 1, f"Inferences{suffix}")
            )
            client_error_metrics.append(
                _metric("InferenceClientErrors", dims, "Sum", 5, f"ClientErrors{suffix}")
            )
            total_input_metrics.append(
                _metric("TotalInputTokens", dims, "Sum", 5, f"Input{suffix}")
            )
            total_output_metrics.append(
                _metric("TotalOutputTokens", dims, "Sum", 5, f"Output{suffix}")
            )

        # Row 1: 调用量 | 客户端错误
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Inferences 调用量趋势（按 Model）",
                left=inference_metrics,
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="InferenceClientErrors 趋势",
                left=client_error_metrics,
                width=12, height=6,
            ),
        )

        # Row 2: Token 使用量
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Token 使用量趋势（TotalInputTokens / TotalOutputTokens）",
                left=total_input_metrics + total_output_metrics,
                width=24, height=6,
            ),
        )

        # Row 3（可选）：按 Project+Model 的逐请求 Token 百分位分布
        if project_ids:
            per_request_metrics = []
            for project_id in project_ids:
                for model_id in model_ids:
                    dims = {"Project": project_id, "Model": model_id}
                    label_suffix = f"{project_id}/{model_id.split('.')[-1][:20]}"
                    per_request_metrics.append(
                        _metric("InputTokens", dims, "p90", 5, f"InputTokens p90 · {label_suffix}")
                    )
                    per_request_metrics.append(
                        _metric("OutputTokens", dims, "p90", 5, f"OutputTokens p90 · {label_suffix}")
                    )
            if per_request_metrics:
                dashboard.add_widgets(
                    cw.GraphWidget(
                        title="按 Project+Model 的逐请求 Token 分布（p90）",
                        left=per_request_metrics,
                        width=24, height=6,
                    ),
                )

        # ─── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "MantleAlarmTopicArn", value=alarm_topic.topic_arn,
                  description="SNS Topic ARN for BedrockMantle alerts")
        CfnOutput(self, "MantleDashboardUrl",
                  value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                        f"?region={self.region}#dashboards:name={dashboard_name}",
                  description="CloudWatch Dashboard URL for BedrockMantle monitoring")
