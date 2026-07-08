# Bedrock Monitoring - 一键部署指引

> 📋 **给 AI 助手的说明**：本文件描述了一个 Amazon Bedrock 监控 CDK 项目的部署任务。请按照下方步骤操作：先确认 AWS 环境，再收集参数，最后完成部署。

---

## 你的任务

帮助用户将 Amazon Bedrock 监控体系部署到他们的 AWS 账号，包括：
- 8 条 CloudWatch 告警（限流、错误、延迟、Token 异常等）
- 一个运营监控 Dashboard
- Bedrock Invocation Logging（可选）

---

## Step 0：确认 AWS 环境

### 0-A 验证 AWS 凭证

运行以下命令，确认凭证有效且账号正确：

```bash
aws sts get-caller-identity
```

预期输出示例：
```json
{
  "UserId": "AIDA...",
  "Account": "123456789012",
  "Arn": "arn:aws:iam::123456789012:user/your-name"
}
```

**如果命令失败或用户不确定用哪个 Profile**，询问用户：
- 是否配置了多个 AWS Profile？（`aws configure list-profiles` 查看）
- 需要使用哪个 Profile？（如 `export AWS_PROFILE=myprofile` 或 `--profile` 参数）
- 是否通过 SSO 登录？（`aws sso login --profile <profile-name>`）

**所需最低 IAM 权限**（如用户不确定，告知其联系 AWS 管理员授权）：
- `cloudwatch:*`（告警、Dashboard）
- `sns:*`（通知 Topic）
- `s3:*`（日志 Bucket，如启用 Logging）
- `logs:*`（Log Group）
- `iam:CreateRole`, `iam:AttachRolePolicy`, `iam:PassRole`
- `bedrock:PutModelInvocationLoggingConfiguration`
- `cloudformation:*`, `ssm:*`（CDK 部署所需）

### 0-B 确认部署 Region（必须显式跟用户确认，不要只用检测到的默认值默默部署）

`aws configure get region` 检测到的值**只是当前 CLI 默认值，不代表用户想部署在这个 Region**。必须像收集邮箱/model_ids 一样，明确问用户：

> 「检测到你当前 AWS CLI 默认 Region 是 `<检测值>`，本次监控要部署在哪个 Region？（如果你的 Bedrock 模型调用发生在别的 Region，请填那个 Region，不要用默认值）」

**如果本次要监控的模型走 `bedrock-mantle` 端点**（GPT-5.4 / GPT-5.5 等 openai-compatible 模型，见下方专门章节），**还需要额外核实该模型在目标 Region 是否真的支持 `bedrock-mantle`**——`aws bedrock list-foundation-models` 查不到这些模型，必须去对应模型卡片文档页的 "Regional Availability" 表核实。**不同模型支持的 Region 范围不一样**，不能互相假设：

| 模型 | Model ID | 支持 Region（In-Region，截至本文档编写时） |
|---|---|---|
| GPT-5.5 | `openai.gpt-5.5` | `us-east-1`、`us-east-2` |
| GPT-5.4 | `openai.gpt-5.4` | `us-east-1`、`us-east-2`、`us-west-2`、`us-gov-west-1`（GovCloud） |

两个模型都仅支持 In-Region 推理，Geo/Global Cross-Region 均不支持。核实方式：

```bash
# 示例：GPT-5.5 / GPT-5.4 的模型卡片文档
# https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html
# https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-54.html
# 查看各自的 "Regional Availability" 表，确认目标 Region 在 In-Region 列是绿色勾
```

> 上表可能随 AWS 发布新 Region/新模型而变化，部署前建议跟进文档页重新确认一遍，不要凭记忆。

> ⚠️ 如果目标 Region 不支持该模型的 bedrock-mantle 端点，告警会一直是 `INSUFFICIENT_DATA`，客户会误以为部署失败——一定要在部署前确认清楚，不要部署完再让客户自己排查。

### 0-C 检查现有环境（避免冲突）

运行以下命令检查现有资源，**根据结果调整部署策略**（`$REGION` 使用 0-B 中已跟用户确认过的值，不要重新用默认值）：

```bash
REGION="<0-B 中确认的 Region>"

# 1. 检查 Bedrock Invocation Logging 是否已启用
echo "=== Bedrock Invocation Logging ==="
aws bedrock get-model-invocation-logging-configuration --region $REGION 2>&1

# 2. 检查是否已有同名告警
echo "=== Existing Bedrock Alarms ==="
aws cloudwatch describe-alarms \
  --alarm-name-prefix "Bedrock-" \
  --region $REGION \
  --query 'MetricAlarms[].AlarmName' --output table 2>&1

# 3. 检查是否已有同名 Dashboard
echo "=== Existing Dashboards ==="
aws cloudwatch list-dashboards \
  --dashboard-name-prefix "Bedrock-" \
  --region $REGION \
  --query 'DashboardEntries[].DashboardName' --output table 2>&1

# 4. 检查 CDK bootstrap 状态
echo "=== CDK Bootstrap ==="
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws cloudformation describe-stacks \
  --stack-name CDKToolkit \
  --region $REGION \
  --query 'Stacks[0].StackStatus' --output text 2>&1
```

**根据输出结果处理冲突**：

| 检测到的情况 | 处理方式 |
|------------|---------|
| Invocation Logging **已启用**，目标和本项目相同 | 在 `cdk.json` 中设置 `"enable_invocation_logging": false`，跳过 Logging 部署，告知用户已有配置会保留 |
| Invocation Logging **已启用**，目标不同 | 询问用户是否要覆盖；如否则同上设为 `false` |
| 已有 `Bedrock-*` 同名告警 | CDK 会直接更新（幂等），无需处理，告知用户 |
| 已有同名 Dashboard | CDK 会直接覆盖更新，告知用户 |
| CDKToolkit 不存在（bootstrap 未做） | Step 3 中需要先执行 `cdk bootstrap` |
| CDKToolkit 已存在 | Step 3 中跳过 `cdk bootstrap` |

---

## ⚠️ 关于 CloudWatch ModelId 维度（必读）

`model_ids` 和 `model_quotas` 里填写的 ModelId **必须与 CloudWatch 实际使用的维度一致**，否则告警和 Dashboard 指标为空。

| 调用方式 | CloudWatch 中的 ModelId 示例 |
|---------|---------------------------|
| 标准 On-Demand | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| Global / Cross-Region Inference | `global.anthropic.claude-sonnet-4-6` |
| Provisioned Throughput | `arn:aws:bedrock:...:provisioned-model/...` |

**如何确认实际 ModelId**：先让用户运行一条命令，展示当前账号下实际出现的 ModelId（`$REGION` 用 0-B 中确认过的部署 Region，不要用当前 CLI 默认值，两者可能不一致）：

```bash
aws cloudwatch list-metrics \
  --namespace AWS/Bedrock \
  --region $REGION \
  --query 'Metrics[].Dimensions[?Name==`ModelId`].Value' \
  --output text | tr '\t' '\n' | sort -u
```

以输出结果为准填写 `model_ids`。**尤其是使用 Global Inference Profile 的用户，一定要用 `global.` 前缀的 ModelId。**

---

## ⚠️ 关于 BedrockMantle 端点（GPT-5.5 等 openai-compatible 模型必读）

如果客户要监控的模型是通过 **`bedrock-mantle` 端点**调用的（Responses API / Chat Completions API / Anthropic Messages API，典型代表：`openai.gpt-5.5`、`openai.gpt-5.4`），
**上面第 0～3 步的告警和 Dashboard（namespace `AWS/Bedrock`）对这些模型完全无效**，因为：

- `bedrock-mantle` 发布指标到独立的 `AWS/BedrockMantle` namespace，`AWS/Bedrock` 下的告警/Dashboard 看不到任何数据
- 指标名称也不同：`Inferences`（非 `Invocations`）、`InferenceClientErrors`（非 `InvocationClientErrors`）、`TotalInputTokens`/`TotalOutputTokens`（非 `InputTokenCount`/`OutputTokenCount`）
- **目前 BedrockMantle 还没有发布延迟类指标**（无 `InvocationLatency`/`TimeToFirstToken` 等效指标），也**没有 ServerErrors/Throttles 指标**——这不是本项目的缺陷，是 AWS 当前的产品限制，如实告知客户即可，不要试图伪造这些告警。

**判断方式**：先确认客户要监控的模型是否走 `bedrock-mantle` 端点（可查模型卡片的 "Endpoints supported" 一栏，或看调用代码用的是 Responses API / Chat Completions API）。是的话，走 Step 1-M 收集 Mantle 参数；不是的话跳过本节。

### Step 1-M：向用户收集 BedrockMantle 监控参数

| # | 参数 | 说明 | 示例 |
|---|------|------|------|
| 1 | **要监控的 Mantle 模型 ID** | 对应 `mantle_model_ids`，可多个，**一定要问到具体版号**，不要假设只有 GPT-5.5（目前已知 `openai.gpt-5.5`、`openai.gpt-5.4` 两个，可同时填多个） | `["openai.gpt-5.5", "openai.gpt-5.4"]` |
| 2 | **告警接收邮箱** | 对应 `mantle_notification_email`；留空则复用 `notification_email` | `ops@company.com` |
| 3 | **Project ID**（可选） | 若使用了 Bedrock Project，填对应 Project ID 可在 Dashboard 上看按 Project+Model 的逐请求 token 分布（p90）；不填则只看 Account/Model 级汇总 | `["proj-abc"]` |
| 4 | **InferenceClientErrors 告警阈值** | 对应 `mantle_client_error_threshold`，默认 50（5 分钟窗口 Sum） | `50` |
| 5 | **是否开启“调用掉零”告警**（可选） | 对应 `mantle_enable_zero_traffic_alarm`，默认 `false`。**只在客户确认该模型应该有持续稳定调用时才建议开启**，否则低频调用场景会持续误报 | `false` |

**确认实际 Model 维度值**（与 runtime 类似，先跑一遍确认，避免指标为空；`$REGION` 仍用 0-B 确认过的值）：

```bash
aws cloudwatch list-metrics \
  --namespace AWS/BedrockMantle \
  --region $REGION \
  --query 'Metrics[].Dimensions[?Name==`Model`].Value' \
  --output text | tr '\t' '\n' | sort -u
```

### Step 2-M：更新 cdk.json（新增字段，不影响原有 runtime 字段）

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

> 同时填多个 Mantle 模型时，告警和 Dashboard 会按模型分别生成，互不影响。但要注意不同模型支持的 Region 可能不一样（见上方 0-B 表格），若其中一个模型在目标 Region 不支持，对应告警会一直 `INSUFFICIENT_DATA`，部署前请逐个核实。

> `mantle_model_ids` 留空（`[]`）则 `BedrockMantleMonitoringStack` **不会被部署**，与原有 runtime 监控 Stack 完全独立，互不影响，`cdk deploy` 会自动跳过。

---

## Step 1：向用户收集以下信息

请逐一询问用户（中文），并在收集完毕后**一次性确认**再执行操作：

| # | 参数 | 说明 | 示例 |
|---|------|------|------|
| 1 | **告警接收邮箱** | 告警触发时发送到哪个邮箱 | `ops@company.com` |
| 2 | **要监控的模型 ID** | 可以多个，来自 Amazon Bedrock 控制台的 Model ID | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| 3 | **各模型 TPM 配额** | 每个模型单独配置，在 AWS Service Quotas 中查询，搜索模型名称 + "tokens per minute" | `{"model-id": 100000}` |
| 4 | **是否启用 Invocation Logging** | 推荐开启（Step 0-C 未检测到冲突时）；已有冲突则跳过 | `是 / 否` |
| 5 | **日志保留天数**（如开启 Logging） | 支持 7 / 14 / 30 / 60 / **90** / 180 / 365 天，默认 90 | `90` |
| 6 | **已有日志 S3 Bucket 名**（可选） | 如已有 Bucket 希望复用，填写 Bucket 名；留空则自动创建 | `my-existing-bucket` |

> 💡 **如何查询 TPM 配额**：AWS Console → Service Quotas → Amazon Bedrock → 搜索模型名 → 找到 "Tokens per minute" 对应的值。

---

## Step 2：更新 cdk.json

根据用户的回答，更新项目根目录下的 `cdk.json` 文件中的 `context` 字段：

```json
{
  "context": {
    "notification_email": "<用户邮箱>",
    "model_ids": ["<模型ID1>", "<模型ID2>"],
    "model_quotas": {
      "<模型ID1>": <TPM配额数字>,
      "<模型ID2>": <TPM配额数字>
    },
    "enable_invocation_logging": <true 或 false>,
    "invocation_log_retention_days": <保留天数>,
    "invocation_s3_bucket_name": "<已有Bucket名，或留空>"
  }
}
```

---

## Step 3：执行部署

```bash
pip install -r requirements.txt

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="<0-B 中确认的 Region，不要用默认值>"

# 仅在 Step 0-C 确认未 bootstrap 时执行
cdk bootstrap aws://$ACCOUNT_ID/$REGION

# 部署（如用户有多个 Profile，在此加 --profile <name>）
# 若 cdk.json 中配置了 mantle_model_ids，cdk deploy 会一并部署 BedrockMonitoringStack 和 BedrockMantleMonitoringStack 两个 Stack；
# 未配置则只部署 BedrockMonitoringStack。
cdk deploy --all --context account=$ACCOUNT_ID --context region=$REGION
```

---

## Step 4：部署完成后告知用户

1. **确认订阅邮件**：收件箱找到 "AWS Notification - Subscription Confirmation"，**必须点击确认**才能收到告警。如部署了 BedrockMantleMonitoringStack，会收到两封确认邮件（runtime + mantle 两个 SNS Topic各自一封）。
2. **查看 Dashboard**：部署输出中的 `DashboardUrl`（runtime）和 `MantleDashboardUrl`（Mantle）分别直接访问。
3. **验证告警**：CloudWatch → Alarms，状态为 `OK` 或 `Insufficient data` 均正常（有对应调用后才产生数据）。
   - 若部署了 Mantle，注意 BedrockMantle 目前没有延迟类告警，**只会有 InferenceClientErrors（可选加 “调用掉零”）两类**，不要向客户承诺延迟/ServerError 告警。

---

## 异常处理

| 问题 | 解决方法 |
|------|---------|
| `aws sts get-caller-identity` 报错 | 检查 `~/.aws/credentials` 或重新执行 `aws configure` / SSO 登录 |
| `cdk bootstrap` 报 IAM 权限不足 | 需要 `AdministratorAccess` 或至少 CloudFormation + IAM 权限，联系 AWS 管理员 |
| Invocation Logging 部署失败（已有配置冲突） | 将 `enable_invocation_logging` 改为 `false` 重新部署，或在 Bedrock 控制台手动关闭后重试 |
| 告警一直是 `Insufficient data` | 正常，需要有 Bedrock 调用才会产生指标数据 |
| 收不到告警邮件 | 检查垃圾箱，找 "AWS Notification - Subscription Confirmation" 并确认 |
