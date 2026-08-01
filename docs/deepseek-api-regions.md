# DeepSeek API 国内站与国际站调研

调研日期：2026-08-01

## 结论

截至调研日，DeepSeek **没有公开两套分别面向中国大陆与海外的 API 站点**。中文站与英文站只是同一官网、同一文档和同一开放平台的语言入口，均指向：

- 开放平台：`https://platform.deepseek.com`
- OpenAI 兼容 API：`https://api.deepseek.com`
- Anthropic 兼容 API：`https://api.deepseek.com/anthropic`
- 模型列表：`GET https://api.deepseek.com/models`

因此，它与 Kimi 的 `.cn` / `.ai` 双平台场景不同。Waku 不需要新增 `deepseek_cn` / `deepseek_global`，也不需要按区域切换 Base URL 或 API Key。

需要把两个概念分开：

1. **没有公开独立区域端点**：官方没有公布第二个中国区或国际区 API 域名。
2. **不保证所有司法辖区都可用**：DeepSeek 的英文开放平台条款明确说明，不保证服务在特定司法辖区可用或持续可用，功能也可能因司法辖区而异。这是准入与合规边界，不是两套 API 技术端点。[官方开放平台条款 §1.4](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)

## 一手证据

### 平台入口没有分区

DeepSeek 中文官网的“API 开放平台”与英文官网的“Access API / DeepSeek Platform”都链接到相同的 `platform.deepseek.com`：

- [DeepSeek 中文官网](https://www.deepseek.com/)
- [DeepSeek 英文官网](https://www.deepseek.com/en/)

中文与英文 API 快速开始页也给出完全相同的 API Base URL 和同一个 Key 申请入口：

- [中文：首次调用 API](https://api-docs.deepseek.com/zh-cn/)
- [英文：Your First API Call](https://api-docs.deepseek.com/)

官方状态页也只有一个公开入口：[`status.deepseek.com`](https://status.deepseek.com/)。未发现按 CN / Global 拆分的官方状态页或 API 服务名。

### API Key 没有公开的区域边界

官方中英文文档都只提供同一个 Key 申请入口 `platform.deepseek.com`，并用同一个 `DEEPSEEK_API_KEY` 调用 `api.deepseek.com`。开放平台条款把它描述为通过统一账号创建、用于调用“开放平台 API 接口”的凭证：[官方 API 认证说明](https://api-docs.deepseek.com/api/deepseek-api/)、[官方开放平台条款 §2](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)。

所以不存在可验证的“中国 Key 是否能用于国际端点”问题：官方只公开一个发 Key 的平台和一个 API 凭证域。更严谨地说，官方没有文档声明“跨区互通”，而是根本没有公开第二个区域 Key 域可供互通测试。

支付方式、实名要求或某些功能可能因用户所在地和页面语言不同，但不能据此推导出 API Key 或 API 域名分区；官方条款反而把服务描述为统一、标准化的开放平台服务。[官方开放平台条款 §1.2](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)

## 协议格式与接口

### OpenAI 兼容格式

官方默认示例使用 OpenAI Chat Completions 格式：

```text
base_url = https://api.deepseek.com
POST /chat/completions
Authorization: Bearer <DEEPSEEK_API_KEY>
```

来源：[官方快速开始](https://api-docs.deepseek.com/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。

### Anthropic 兼容格式

同一个 Key 也可通过 Anthropic Messages 格式调用：

```text
base_url = https://api.deepseek.com/anthropic
x-api-key: <DEEPSEEK_API_KEY>
```

官方兼容表说明 `x-api-key` 完整支持，并列出了 Messages、thinking 与工具字段的兼容范围：[Using the Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api/)。这属于同一平台上的另一种 wire format，不是国际站或国内站。

### 模型列表

官方 API Reference 定义：

```http
GET https://api.deepseek.com/models
Authorization: Bearer <DEEPSEEK_API_KEY>
```

响应为 OpenAI 风格的 `{"object":"list","data":[...]}`，当前文档示例包含 `deepseek-v4-flash` 与 `deepseek-v4-pro`：[Lists Models](https://api-docs.deepseek.com/api/list-models/)。

官方 Anthropic 兼容文档只描述 Messages 兼容能力，**没有公布 Anthropic Models API 路径**。因此即使聊天走 `https://api.deepseek.com/anthropic`，模型目录也应继续请求官方明确记录的 `https://api.deepseek.com/models`，不应依赖未文档化的 `/anthropic/v1/models`。

## 只读 HTTP 探测

2026-08-01 在无 API Key 情况下，对官方域名执行 GET：

```text
GET https://api.deepseek.com/models                -> 401 Authentication Fails
GET https://api.deepseek.com/v1/models             -> 401 Authentication Fails
GET https://api.deepseek.com/anthropic/v1/models   -> 401 Authentication Fails
```

这说明鉴权在路径路由前统一执行；它不能证明后两个路径在有效鉴权后可用。接口是否受支持仍应以官方 API Reference 为准，目前只有 `/models` 被正式记录。本次探测未发送任何有效 Key，也没有产生模型调用费用。

## 对 Waku 当前配置的建议

Waku 当前配置是：

```python
"deepseek": Provider(
    "openai",
    "DEEPSEEK_API_KEY",
    "https://api.deepseek.com",
    "deepseek-v4-pro",
    "deepseek-v4-pro",
)
```

该配置与官方文档一致。`catalog.list_models()` 会对 OpenAI wire provider 自动拼接 `{base_url}/models`，最终请求 `https://api.deepseek.com/models`，也与官方模型列表接口一致。

建议：

1. **保持单一 `deepseek` provider**，不要增加区域字段、`deepseek_cn` 或 `deepseek_global`。
2. **保持单一 `DEEPSEEK_API_KEY`**，不要为不存在的官方区域 Key 域增加额外环境变量。
3. 当前 OpenAI wire 配置无需因“国内/国际站”调整。
4. 若未来为了更贴合 Waku 内部的 Anthropic Messages 形状而改为 Anthropic wire，应同时配置：

   ```python
   base_url="https://api.deepseek.com/anthropic"
   catalog_url="https://api.deepseek.com/models"
   ```

   这是协议切换，不是区域切换。
5. 只接受 DeepSeek 官方文档列出的 `deepseek.com` 子域。不要因第三方声称存在“国内专线”而把 Key 发送到拼写相近或非官方域名。

## 证据边界

- 本结论覆盖 DeepSeek 官方托管 API，不包括火山引擎、阿里云、OpenRouter 等第三方托管的 DeepSeek 模型；这些服务有自己的域名和 Key，不能称为 DeepSeek 官方“海外站”。
- 没有使用两把所谓区域 Key 做互通实验，因为官方没有提供第二个区域 Key 平台。
- 若 DeepSeek 将来公布新的区域端点，应以中英文快速开始页、API Reference 和官方状态页的更新为准。
