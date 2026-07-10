# Bedrock Monitoring CDK

一键部署 Amazon Bedrock 监控体系，包含 **8 条 CloudWatch 告警**、**运营大盘 Dashboard**、以及可选的 **Invocation Logging**。

## 前置条件

```bash
# 推荐使用虚拟环境（避免依赖冲突）
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
npm install -g aws-cdk
cdk bootstrap  # 首次使用需要，已 bootstrap 过可跳过
```

> **venv 用户注意**：激活虚拟环境后直接运行 `cdk` 命令即可。如果遇到 `cdk synth` 报找不到模块，将 `cdk.json` 中的 `"app": "python app.py"` 改为 `"app": ".venv/bin/python app.py"`（Windows 改为 `.venv\\Scripts\\python app.py`）。

## 填写参数（cdk.json）

| 参数 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `notification_email` | ✅ | 告警接收邮箱 | `alerts@company.com` |
| `model_ids` | ✅ | 要监控的 Bedrock 模型 ID 列表 | `["anthropic.claude-3-5-sonnet-20241022-v2:0"]` |
| `model_quotas` | ✅ | **每个模型**各自的 TPM 配额（必须与 model_ids 一一对应，否则 synth 报错） | 见下方示例 |
| `input_token_avg_threshold` | 可选 | InputToken 异常告警阈值（默认 50000） | `50000` |
| `dashboard_name` | 可选 | Dashboard 名称（默认 `Bedrock-Operations`） | `Bedrock-Operations` |
| `enable_invocation_logging` | 可选 | 是否启用 Invocation Logging（默认 `true`） | `true` |
| `invocation_log_retention_days` | 可选 | 日志保留天数，支持 7/14/30/60/90/180/365（默认 90） | `90` |
| `invocation_s3_bucket_name` | 可选 | 已有 S3 bucket 名，留空则自动创建 | `""` |

**cdk.json 示例（多模型）：**
```json
{
  "context": {
    "notification_email": "alerts@company.com",
    "model_ids": [
      "anthropic.claude-3-5-sonnet-20241022-v2:0",
      "anthropic.claude-3-haiku-20240307-v1:0"
    ],
    "model_quotas": {
      "anthropic.claude-3-5-sonnet-20241022-v2:0": 200000,
      "anthropic.claude-3-haiku-20240307-v1:0": 500000
    }
  }
}
```

> ⚠️ **TPM 配额查询**：AWS Console → Service Quotas → Amazon Bedrock → 搜索模型名 → 找到 "Tokens per minute"。

## ⚠️ 关于 CloudWatch ModelId 维度

CloudWatch 中记录的 `ModelId` 取决于实际调用方式：

| 调用方式 | CloudWatch 中的 ModelId 示例 |
|---------|---------------------------|
| 标准 On-Demand | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| Global / Cross-Region Inference | `global.anthropic.claude-sonnet-4-6` |
| Provisioned Throughput | `arn:aws:bedrock:...` |

**如果你使用 Global Inference Profile（如 `global.anthropic.claude-sonnet-4-6`），`model_ids` 和 `model_quotas` 必须使用 `global.` 前缀的 ModelId**，否则告警和 Dashboard 的指标维度不匹配，数据为空。

验证方法：在 CloudWatch → Metrics → AWS/Bedrock 中，查看实际出现的 ModelId 维度值。

## 部署

```bash
# 激活虚拟环境后执行
cdk deploy --context account=$(aws sts get-caller-identity --query Account --output text)

# 部署完成后检查邮箱，确认 SNS 订阅
```

## 部署内容

| 资源 | 说明 |
|------|------|
| SNS Topic | `bedrock-monitoring-alerts`，告警统一投递 |
| CloudWatch Alarms | **7 条模型级告警 × 模型数 + 1 条账户级 LogDelivery 告警**（例：2 个模型 = 15 条） |
| CloudWatch Dashboard | `Bedrock-Operations`，含告警状态 + 指标图表 + Prompt Cache Hit Rate |
| S3 Bucket | Invocation 日志存储（`enable_invocation_logging: true` 时创建） |
| CloudWatch Log Group | `/aws/bedrock/invocation-logs`（同上） |
| IAM Role | Bedrock 服务写日志权限 |
| Custom Resource | 自动调用 Bedrock API 启用 Invocation Logging |

## 删除

```bash
cdk destroy
```

> ⚠️ S3 Bucket 和 CloudWatch Log Group 使用 `RETAIN` 策略，`cdk destroy` 不会删除，需手动清理。

---

## BedrockMantle 监控（可选，独立 Stack）

如果你有模型走 **`bedrock-mantle` 端点**（Responses API / Chat Completions API / Anthropic Messages API，典型代表 `openai.gpt-5.5`、`openai.gpt-5.4`），上面这套监控**完全覆盖不到**——`bedrock-mantle` 指标发布在独立的 `AWS/BedrockMantle` namespace，且指标名称、维度都跟 `AWS/Bedrock` 不同。

本项目提供独立的 `BedrockMantleMonitoringStack`，与上面的 runtime 监控完全解耦（独立 SNS Topic、独立 Dashboard、独立告警），互不影响。只需在 `cdk.json` 中填写 `mantle_model_ids`（留空则该 Stack 不部署，**可同时填多个型号**）：

```json
{
  "context": {
    "mantle_model_ids": ["openai.gpt-5.5", "openai.gpt-5.4"],
    "mantle_project_ids": [],
    "mantle_notification_email": "ops@company.com",
    "mantle_client_error_threshold": 50,
    "mantle_dashboard_name": "Bedrock-Mantle-Operations",
    "mantle_enable_zero_traffic_alarm": false
  }
}
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `mantle_model_ids` | 部署本 Stack 时必填 | 走 bedrock-mantle 端点的模型 ID，可多个，如 `openai.gpt-5.5`、`openai.gpt-5.4` |

**已知模型与 Region 对应关系**（截至本文档编写时，部署前建议去对应模型卡片重新确认）：

| 模型 | Model ID | 支持 Region（In-Region） |
|---|---|---|
| GPT-5.5 | `openai.gpt-5.5` | `us-east-1`、`us-east-2` |
| GPT-5.4 | `openai.gpt-5.4` | `us-east-1`、`us-east-2`、`us-west-2`、`us-gov-west-1`（GovCloud） |

其他参数：
| `mantle_project_ids` | 可选 | 若使用了 Bedrock Project，填写后 Dashboard 会新增按 Project+Model 的逐请求 token 百分位（p90）图表 |
| `mantle_notification_email` | 可选 | 告警邮箱，留空则复用 `notification_email` |
| `mantle_client_error_threshold` | 可选 | InferenceClientErrors 5 分钟窗口 Sum 阈值，默认 50 |
| `mantle_dashboard_name` | 可选 | 默认 `Bedrock-Mantle-Operations` |
| `mantle_enable_zero_traffic_alarm` | 可选 | 是否开启“调用掉零”告警，默认 `false`（低频调用场景开启会持续误报，只在确认应有稳定调用时开启）|

**已知能力边界（AWS 现状，非本项目缺陷）**：
- ❌ 暂无 `InvocationLatency` / `TimeToFirstToken` 等效延迟指标
- ❌ 暂无 ServerErrors、Throttles 指标
- ✅ 只有 `InferenceClientErrors`（4xx）可配告警
- ✅ `Inferences`、`TotalInputTokens`、`TotalOutputTokens` 可用于 Dashboard 趋势图
- ✅ 若使用了 Project 维度，可看按 Project+Model 的逐请求 Token p90 分布

部署时 `cdk deploy --all` 会一并部署（未配置 `mantle_model_ids` 时自动跳过）。删除同样走 `cdk destroy`。

## 常见问题

**Q: `cdk synth` 报错提示缺少某个模型的 quota**  
A: `model_quotas` 中必须包含 `model_ids` 里的每一个模型，检查是否有遗漏或拼写不一致。

**Q: Dashboard 中某个模型数据为空**  
A: 检查 `model_ids` 中填写的 ModelId 是否与 CloudWatch 实际使用的维度一致（尤其是 Global Inference Profile 需要 `global.` 前缀）。

**Q: 收不到告警邮件**  
A: 检查 SNS 订阅是否已确认（部署后会收到 confirmation 邮件，注意查看垃圾箱）。

**Q: EstimatedTPMQuotaUsage 告警频繁触发**  
A: 实际流量接近配额，联系 AWS SA 申请提额，或检查是否配置了过高的 `max_tokens`（会影响配额预留）。

**Q: Invocation Logging 部署失败**  
A: 检查当前账号/Region 的 Bedrock Invocation Logging 是否已手动开启（会冲突），先在控制台关闭再重新部署，或将 `enable_invocation_logging` 设为 `false`。
