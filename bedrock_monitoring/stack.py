from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_s3 as s3,
    aws_logs as logs,
    aws_iam as iam,
    custom_resources as cr,
)
from constructs import Construct

# CloudWatch Logs Retention 枚举映射（仅支持固定值）
_RETENTION_MAP = {
    7:   logs.RetentionDays.ONE_WEEK,
    14:  logs.RetentionDays.TWO_WEEKS,
    30:  logs.RetentionDays.ONE_MONTH,
    60:  logs.RetentionDays.TWO_MONTHS,
    90:  logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    731: logs.RetentionDays.TWO_YEARS,
}


class BedrockMonitoringStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── 读取 context 参数 ────────────────────────────────────────────────
        notification_email: str = self.node.try_get_context("notification_email") or ""
        model_ids: list = self.node.try_get_context("model_ids") or []
        # 按模型配置各自的 TPM 配额，格式：{"model-id": quota_int, ...}
        model_quotas: dict = self.node.try_get_context("model_quotas") or {}
        input_token_threshold: int = int(
            self.node.try_get_context("input_token_avg_threshold") or 50000
        )
        dashboard_name: str = (
            self.node.try_get_context("dashboard_name") or "Bedrock-Operations"
        )
        enable_logging: bool = self.node.try_get_context("enable_invocation_logging") is not False
        log_group_name: str = (
            self.node.try_get_context("invocation_log_group_name")
            or "/aws/bedrock/invocation-logs"
        )
        log_retention_days: int = int(
            self.node.try_get_context("invocation_log_retention_days") or 90
        )
        s3_bucket_name: str = self.node.try_get_context("invocation_s3_bucket_name") or ""

        # ─── SNS Topic ────────────────────────────────────────────────────────
        alarm_topic = sns.Topic(
            self,
            "BedrockAlarmTopic",
            topic_name="bedrock-monitoring-alerts",
            display_name="Bedrock Monitoring Alerts",
        )
        if notification_email:
            alarm_topic.add_subscription(subscriptions.EmailSubscription(notification_email))

        alarm_action = cw_actions.SnsAction(alarm_topic)

        # ─── 校验：model_quotas 必须覆盖 model_ids 中每个模型 ─────────────────
        missing_quotas = [m for m in model_ids if m not in model_quotas]
        if missing_quotas:
            raise ValueError(
                f"cdk.json 中 model_quotas 缺少以下模型的 TPM 配额配置，请补全后重新 synth：\n"
                + "\n".join(f"  - {m}" for m in missing_quotas)
                + "\n\n提示：若使用 Global Inference Profile，ModelId 应使用 global. 前缀"
                + "（如 global.anthropic.claude-sonnet-4-6）。"
                + "\n可在 CloudWatch → Metrics → AWS/Bedrock 中确认实际使用的 ModelId 维度。"
            )

        # ─── 告警辅助函数（在循环外定义，通过参数传入 dims）─────────────────
        def _make_alarm(
            alarm_id: str,
            alarm_name: str,
            metric_name: str,
            statistic: str,
            period_min: int,
            threshold: float,
            description: str,
            dims: dict,
            eval_periods: int = 1,
        ) -> cw.Alarm:
            a = cw.Alarm(
                self,
                alarm_id,
                alarm_name=alarm_name,
                alarm_description=description,
                metric=cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name=metric_name,
                    dimensions_map=dims,
                    statistic=statistic,
                    period=Duration.minutes(period_min),
                ),
                threshold=threshold,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=eval_periods,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(alarm_action)
            return a

        # ─── 告警（按模型循环）────────────────────────────────────────────────
        all_alarms: list[cw.Alarm] = []

        for model_id in model_ids:
            safe = model_id.replace(".", "-").replace(":", "-").replace("/", "-")
            dims = {"ModelId": model_id}

            # P0: InvocationThrottles > 0, 1min
            all_alarms.append(_make_alarm(
                f"ThrottlesAlarm-{safe}", f"Bedrock-InvocationThrottles-{safe}",
                "InvocationThrottles", "Sum", 1, 0,
                f"[P0] {model_id} - 请求被节流，配额不足，直接影响业务", dims,
            ))

            # P0: InvocationServerErrors > 5, 5min
            all_alarms.append(_make_alarm(
                f"ServerErrorsAlarm-{safe}", f"Bedrock-ServerErrors-{safe}",
                "InvocationServerErrors", "Sum", 5, 5,
                f"[P0] {model_id} - 服务端 5xx 错误 > 5，可能是 AWS 侧问题", dims,
            ))

            # P1: EstimatedTPMQuotaUsage > 80% of quota, 1min
            tpm_threshold = int(model_quotas[model_id] * 0.8)
            all_alarms.append(_make_alarm(
                f"TPMQuotaAlarm-{safe}", f"Bedrock-TPMQuotaUsage-High-{safe}",
                "EstimatedTPMQuotaUsage", "Sum", 1, tpm_threshold,
                f"[P1] {model_id} - TPM 配额消耗 > 80%（阈值 {tpm_threshold} tokens），提前预警", dims,
            ))

            # P1: TimeToFirstToken P99 > 5000ms, 5min
            all_alarms.append(_make_alarm(
                f"TTFTAlarm-{safe}", f"Bedrock-TTFT-P99-High-{safe}",
                "TimeToFirstToken", "p99", 5, 5000,
                f"[P1] {model_id} - 流式首 Token 延迟 P99 > 5s，影响用户体验", dims,
                eval_periods=2,
            ))

            # P1: InvocationLatency P99 > 30000ms, 5min
            all_alarms.append(_make_alarm(
                f"LatencyAlarm-{safe}", f"Bedrock-Latency-P99-High-{safe}",
                "InvocationLatency", "p99", 5, 30000,
                f"[P1] {model_id} - 端到端推理延迟 P99 > 30s", dims,
                eval_periods=2,
            ))

            # P2: InvocationClientErrors > 50, 5min
            all_alarms.append(_make_alarm(
                f"ClientErrorsAlarm-{safe}", f"Bedrock-ClientErrors-High-{safe}",
                "InvocationClientErrors", "Sum", 5, 50,
                f"[P2] {model_id} - 客户端 4xx 错误突增 > 50，检查参数/权限/模型配置", dims,
            ))

            # P2: InputTokenCount Average > threshold, 5min
            all_alarms.append(_make_alarm(
                f"InputTokenAlarm-{safe}", f"Bedrock-InputTokenCount-Anomaly-{safe}",
                "InputTokenCount", "Average", 5, input_token_threshold,
                f"[P2] {model_id} - 平均 InputToken > {input_token_threshold}，检测异常大请求", dims,
            ))

        # P2: InvocationLogDeliveryFailure（账户级别，无 ModelId 维度）
        log_delivery_alarm = cw.Alarm(
            self,
            "LogDeliveryAlarm",
            alarm_name="Bedrock-LogDeliveryFailure",
            alarm_description="[P2] Bedrock 日志投递失败，影响审计和排查能力",
            metric=cw.Metric(
                namespace="AWS/Bedrock",
                metric_name="InvocationLogDeliveryFailure",
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=0,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        log_delivery_alarm.add_alarm_action(alarm_action)
        all_alarms.append(log_delivery_alarm)

        # ─── CloudWatch Dashboard ─────────────────────────────────────────────
        dashboard = cw.Dashboard(
            self,
            "BedrockDashboard",
            dashboard_name=dashboard_name,
        )

        # Row 0: 告警状态汇总
        dashboard.add_widgets(
            cw.AlarmStatusWidget(
                title="📋 告警状态汇总",
                alarms=all_alarms,
                width=24,
                height=3,
            )
        )

        # 按模型收集各类指标
        invocation_metrics, latency_metrics, ttft_metrics = [], [], []
        tpm_metrics, token_metrics = [], []
        cache_read_metrics, cache_write_metrics, cache_hit_rate_exprs = [], [], []

        for model_id in model_ids:
            suffix = f" · {model_id.split('.')[-1][:24]}" if len(model_ids) > 1 else ""
            dims = {"ModelId": model_id}

            invocation_metrics += [
                cw.Metric(namespace="AWS/Bedrock", metric_name="Invocations",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(1), label=f"Invocations{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationThrottles",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(1), label=f"Throttles{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationServerErrors",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"ServerErrors{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationClientErrors",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"ClientErrors{suffix}"),
            ]
            latency_metrics += [
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationLatency",
                          dimensions_map=dims, statistic="p50",
                          period=Duration.minutes(5), label=f"P50{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationLatency",
                          dimensions_map=dims, statistic="p90",
                          period=Duration.minutes(5), label=f"P90{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="InvocationLatency",
                          dimensions_map=dims, statistic="p99",
                          period=Duration.minutes(5), label=f"P99{suffix}"),
            ]
            ttft_metrics += [
                cw.Metric(namespace="AWS/Bedrock", metric_name="TimeToFirstToken",
                          dimensions_map=dims, statistic="p50",
                          period=Duration.minutes(5), label=f"P50{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="TimeToFirstToken",
                          dimensions_map=dims, statistic="p99",
                          period=Duration.minutes(5), label=f"P99{suffix}"),
            ]
            tpm_metrics.append(
                cw.Metric(namespace="AWS/Bedrock", metric_name="EstimatedTPMQuotaUsage",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(1), label=f"TPM Usage{suffix}"),
            )
            token_metrics += [
                cw.Metric(namespace="AWS/Bedrock", metric_name="InputTokenCount",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"Input{suffix}"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="OutputTokenCount",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"Output{suffix}"),
            ]
            idx = len(cache_read_metrics)  # 用于 MathExpression 唯一 ID
            cache_read_metrics.append(
                cw.Metric(namespace="AWS/Bedrock", metric_name="CacheReadInputTokenCount",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"Cache Read{suffix}"),
            )
            cache_write_metrics.append(
                cw.Metric(namespace="AWS/Bedrock", metric_name="CacheWriteInputTokenCount",
                          dimensions_map=dims, statistic="Sum",
                          period=Duration.minutes(5), label=f"Cache Write{suffix}"),
            )
            # Cache Hit Rate % = CacheRead / (CacheRead + CacheWrite + Input) × 100
            cache_hit_rate_exprs.append(
                cw.MathExpression(
                    expression=f"100 * r{idx} / (r{idx} + w{idx} + i{idx})",
                    using_metrics={
                        f"r{idx}": cw.Metric(namespace="AWS/Bedrock", metric_name="CacheReadInputTokenCount",
                                             dimensions_map=dims, statistic="Sum", period=Duration.minutes(5)),
                        f"w{idx}": cw.Metric(namespace="AWS/Bedrock", metric_name="CacheWriteInputTokenCount",
                                             dimensions_map=dims, statistic="Sum", period=Duration.minutes(5)),
                        f"i{idx}": cw.Metric(namespace="AWS/Bedrock", metric_name="InputTokenCount",
                                             dimensions_map=dims, statistic="Sum", period=Duration.minutes(5)),
                    },
                    label=f"Cache Hit Rate %{suffix}",
                    period=Duration.minutes(5),
                )
            )

        # Row 1: 调用量 & 错误 | 端到端延迟
        dashboard.add_widgets(
            cw.GraphWidget(
                title="调用量 & 错误总览",
                left=invocation_metrics,
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="端到端延迟分布 (ms)",
                left=latency_metrics,
                left_y_axis=cw.YAxisProps(label="ms"),
                width=12, height=6,
            ),
        )

        # Row 2: TTFT | TPM 配额消耗
        dashboard.add_widgets(
            cw.GraphWidget(
                title="首 Token 延迟 TTFT (ms)",
                left=ttft_metrics,
                left_y_axis=cw.YAxisProps(label="ms"),
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="TPM 配额消耗趋势 (tokens)",
                left=tpm_metrics,
                width=12, height=6,
            ),
        )

        # Row 3: Token 使用量 | Prompt Cache Read vs Write
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Token 使用量趋势",
                left=token_metrics,
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="Prompt Cache Read vs Write (tokens)",
                left=cache_read_metrics + cache_write_metrics,
                width=12, height=6,
            ),
        )

        # Row 4: Prompt Cache Hit Rate %（MathExpression: CacheRead / (CacheRead + CacheWrite + Input)）
        if cache_hit_rate_exprs:
            dashboard.add_widgets(
                cw.GraphWidget(
                    title="Prompt Cache Hit Rate %",
                    left=cache_hit_rate_exprs,
                    left_y_axis=cw.YAxisProps(label="%", min=0, max=100),
                    width=24, height=6,
                ),
            )

        # ─── Invocation Logging（可选）────────────────────────────────────────
        if enable_logging:
            # S3 Bucket
            bucket_id = "BedrockLogBucket"
            if s3_bucket_name:
                log_bucket = s3.Bucket.from_bucket_name(self, bucket_id, s3_bucket_name)
            else:
                log_bucket = s3.Bucket(
                    self,
                    bucket_id,
                    bucket_name=f"bedrock-invocation-logs-{self.account}-{self.region}",
                    removal_policy=RemovalPolicy.RETAIN,
                    lifecycle_rules=[
                        s3.LifecycleRule(
                            id="expire-logs",
                            expiration=Duration.days(log_retention_days),
                        )
                    ],
                )

            # CloudWatch Log Group
            cw_retention = _RETENTION_MAP.get(log_retention_days, logs.RetentionDays.THREE_MONTHS)
            log_group = logs.LogGroup(
                self,
                "BedrockLogGroup",
                log_group_name=log_group_name,
                retention=cw_retention,
                removal_policy=RemovalPolicy.RETAIN,
            )

            # IAM Role：Bedrock 服务写 CW Logs + S3
            bedrock_logging_role = iam.Role(
                self,
                "BedrockLoggingRole",
                assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
                description="Allows Bedrock to deliver invocation logs to CW Logs and S3",
            )
            log_bucket.grant_write(bedrock_logging_role)
            # Bedrock 在启用 Logging 时会先调用 s3:GetBucketAcl 验证 Bucket 归属
            # grant_write() 不包含此权限，需要显式添加
            bedrock_logging_role.add_to_policy(iam.PolicyStatement(
                actions=["s3:GetBucketAcl"],
                resources=[log_bucket.bucket_arn],
            ))
            log_group.grant_write(bedrock_logging_role)

            # 通过 AwsCustomResource 调用 Bedrock API 启用 Invocation Logging
            cr.AwsCustomResource(
                self,
                "BedrockInvocationLoggingConfig",
                on_create=cr.AwsSdkCall(
                    service="Bedrock",
                    action="putModelInvocationLoggingConfiguration",
                    parameters={
                        "loggingConfig": {
                            "cloudWatchConfig": {
                                "logGroupName": log_group.log_group_name,
                                "roleArn": bedrock_logging_role.role_arn,
                                "largeDataDeliveryS3Config": {
                                    "bucketName": log_bucket.bucket_name,
                                    "keyPrefix": "large-data",
                                },
                            },
                            "s3Config": {
                                "bucketName": log_bucket.bucket_name,
                                "keyPrefix": "invocation-logs",
                            },
                            "textDataDeliveryEnabled": True,
                            "imageDataDeliveryEnabled": False,
                            "embeddingDataDeliveryEnabled": False,
                        }
                    },
                    physical_resource_id=cr.PhysicalResourceId.of("BedrockInvocationLoggingConfig"),
                ),
                on_update=cr.AwsSdkCall(
                    service="Bedrock",
                    action="putModelInvocationLoggingConfiguration",
                    parameters={
                        "loggingConfig": {
                            "cloudWatchConfig": {
                                "logGroupName": log_group.log_group_name,
                                "roleArn": bedrock_logging_role.role_arn,
                                "largeDataDeliveryS3Config": {
                                    "bucketName": log_bucket.bucket_name,
                                    "keyPrefix": "large-data",
                                },
                            },
                            "s3Config": {
                                "bucketName": log_bucket.bucket_name,
                                "keyPrefix": "invocation-logs",
                            },
                            "textDataDeliveryEnabled": True,
                            "imageDataDeliveryEnabled": False,
                            "embeddingDataDeliveryEnabled": False,
                        }
                    },
                    physical_resource_id=cr.PhysicalResourceId.of("BedrockInvocationLoggingConfig"),
                ),
                on_delete=cr.AwsSdkCall(
                    service="Bedrock",
                    action="deleteModelInvocationLoggingConfiguration",
                    parameters={},
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=[
                            "bedrock:PutModelInvocationLoggingConfiguration",
                            "bedrock:DeleteModelInvocationLoggingConfiguration",
                        ],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["iam:PassRole"],
                        resources=[bedrock_logging_role.role_arn],
                    ),
                ]),
            )

            CfnOutput(self, "LogBucketName", value=log_bucket.bucket_name,
                      description="Bedrock invocation log S3 bucket")
            CfnOutput(self, "LogGroupName", value=log_group.log_group_name,
                      description="Bedrock invocation log CloudWatch Log Group")

        # ─── Outputs ──────────────────────────────────────────────────────────
        CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn,
                  description="SNS Topic ARN for Bedrock alerts")
        CfnOutput(self, "DashboardUrl",
                  value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                        f"?region={self.region}#dashboards:name={dashboard_name}",
                  description="CloudWatch Dashboard URL")
