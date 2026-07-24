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

### 0-B 检查现有环境（避免冲突）

运行以下命令检查现有资源，**根据结果调整部署策略**：

```bash
REGION=$(aws configure get region || echo "ap-northeast-1")

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

**如何确认实际 ModelId**：先让用户运行一条命令，展示当前账号下实际出现的 ModelId：

```bash
aws cloudwatch list-metrics \
  --namespace AWS/Bedrock \
  --region $(aws configure get region) \
  --query 'Metrics[].Dimensions[?Name==`ModelId`].Value' \
  --output text | tr '\t' '\n' | sort -u
```

以输出结果为准填写 `model_ids`。**尤其是使用 Global Inference Profile 的用户，一定要用 `global.` 前缀的 ModelId。**

---

## Step 1：向用户收集以下信息

请逐一询问用户（中文），并在收集完毕后**一次性确认**再执行操作：

| # | 参数 | 说明 | 示例 |
|---|------|------|------|
| 1 | **告警接收邮箱** | 告警触发时发送到哪个邮箱 | `ops@company.com` |
| 2 | **要监控的模型 ID** | 可以多个，来自 Amazon Bedrock 控制台的 Model ID | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| 3 | **各模型 TPM 配额** | 每个模型单独配置，在 AWS Service Quotas 中查询，搜索模型名称 + "tokens per minute" | `{"model-id": 100000}` |
| 4 | **是否启用 Invocation Logging** | 推荐开启（Step 0-B 未检测到冲突时）；已有冲突则跳过 | `是 / 否` |
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
REGION=$(aws configure get region || echo "ap-northeast-1")

# 仅在 Step 0-B 确认未 bootstrap 时执行
cdk bootstrap aws://$ACCOUNT_ID/$REGION

# 部署（如用户有多个 Profile，在此加 --profile <name>）
cdk deploy --context account=$ACCOUNT_ID
```

---

## Step 4：部署完成后告知用户

1. **确认订阅邮件**：收件箱找到 "AWS Notification - Subscription Confirmation"，**必须点击确认**才能收到告警。
2. **查看 Dashboard**：部署输出中的 `DashboardUrl` 直接点击访问。
3. **验证告警**：CloudWatch → Alarms，状态为 `OK` 或 `Insufficient data` 均正常（有 Bedrock 调用后才产生数据）。

---

## Step 5（可选）：问用户是否需要同时部署 AWS DevOps Agent

部署完告警+Dashboard 后（此时“发现问题”已完成，但流程止于“通知人”），主动问用户：

> “是否需要同时部署 AWS DevOps Agent？部署后告警触发时会自动开始诊断根因并给出缓解方案，不用人工看日志才知道头绪。需要吗？（需注意：目前还是 preview 阶段产品，仅在部分 Region 可用）”

**如果用户同意**，按以下步骤处理：

1. **确认 Region 支持**：目前 AWS DevOps Agent 仅在部分 Region 可用（如 `us-east-1`）。若本项目部署 Region（即 Bedrock 监控所在 Region）不在支持列表中，告知用户只能部署到其他 Region（可能需要另外一个监控账号/Region），不要强行在不支持的 Region 部署。
2. **确认部署范围**：本项目只包含“单账号自监控”（Agent Space + IAM Role + 当前账号关联，AWS 官方教程 Part 1）。如用户需要跨账号监控（Part 2），告知用户本项目不覆盖，需补充官方文档步骤。
3. **修改 `cdk.json`**：将 `deploy_devops_agent` 设为 `true`，可选填写 `devops_agent_space_name`（默认 `BedrockMonitoringAgentSpace`）。
4. **重新执行部署**（不影响已部署的 `BedrockMonitoringStack`）：
   ```bash
   cdk deploy --all --context account=$(aws sts get-caller-identity --query Account --output text)
   ```
5. **部署完告知用户**：
   - 记录输出中的 `AgentSpaceArn`（后续跨账号扩展需要）。
   - **明确告知用户两个边界**：
     - DevOps Agent 现在可以自动诊断告警根因并给出缓解方案，**但不会自动改变生产基础设施/代码**——需要动手的修复方案会以 "agent-ready instructions" 形式交给 Kiro 或人工落地，不是全自动兼底。
     - 还需到 AWS DevOps Agent 控制台/接入对应 CloudWatch 告警为观察数据源、配置 Slack/ServiceNow 等通知通道，才能形成完整闭环——CDK 本身只部署 Agent Space 基础设施，不自动完成这一步。

**如果用户不需要**，保持 `deploy_devops_agent: false`（默认值）即可，不需要任何额外操作。

---

## 异常处理

| 问题 | 解决方法 |
|------|---------|
| `aws sts get-caller-identity` 报错 | 检查 `~/.aws/credentials` 或重新执行 `aws configure` / SSO 登录 |
| `cdk bootstrap` 报 IAM 权限不足 | 需要 `AdministratorAccess` 或至少 CloudFormation + IAM 权限，联系 AWS 管理员 |
| Invocation Logging 部署失败（已有配置冲突） | 将 `enable_invocation_logging` 改为 `false` 重新部署，或在 Bedrock 控制台手动关闭后重试 |
| 告警一直是 `Insufficient data` | 正常，需要有 Bedrock 调用才会产生指标数据 |
| 收不到告警邮件 | 检查垃圾箱，找 "AWS Notification - Subscription Confirmation" 并确认 |
| DevOps Agent「Launch via IAM」报 `AccessDenied ... aidevops:GetAgentSpace` | `DevOpsAgentRole-WebappAdmin` 的 trust policy 必须同时允许 `sts:AssumeRole` **和** `sts:TagSession`（缺 TagSession 会导致 AgentSpaceId 会话标签打不上，ABAC 条件永远不匹配）。本项目已修复并验证通过；若手动改过该角色，检查 trust policy 是否两者都有 |
