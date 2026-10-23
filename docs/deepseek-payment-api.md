# DeepSeek 喂白饭：支付接口与订单处理

核验日期：2026-10-23。适用项目：`astrbot_plugin_dsAutoPay`。

插件让 QQ 群友通过二维码为管理员配置的 DeepSeek 开放平台账户充值，后台查询支付结果，并提供本人订单查询。用户命令、安装与配置见 [README](../README.md)。

本文依据浏览器请求样本、真实创建订单与查单结果整理。以下为 DeepSeek 开放平台网页内部接口，未确认有公开稳定性承诺。所有凭证、订单号及支付链接均使用占位符。

## 1. 验证范围

- 已实际通过 Bearer 登录认证创建 1 元支付宝订单，无需 Cookie、设备字段或自定义 `x-client-*` 请求头。
- 已生成二维码并由用户扫码付款，随后通过相同订单号查得 `SUCCESS`、金额 1、币种 CNY、渠道 ALIPAY 和有效支付时间。
- 微信创建参数来自用户提供的浏览器请求，尚未完成微信真实付款验证。
- 尚未完成 AstrBot + NapCat 运行环境中的群内发码、自动轮询和通知联调。

## 2. 认证与编号

管理员只需配置 `web_token`（DeepSeek 开放平台网页登录令牌）与 `allowed_groups`（开放的 QQ 群号）。

```http
Authorization: Bearer <WEB_TOKEN>
```

令牌来自 `platform.deepseek.com`，不是 DeepSeek 对话网站登录令牌，也不是模型 API Key。插件配置只填写 Bearer 后面的值。

| 标识 | 来源与用途 |
| --- | --- |
| 本地订单号 `DS…` | 插件生成，供群友查询自己的订单 |
| `request_id` | 插件生成并持久化的 UUID，用于标识一次创建请求；上游是否保证幂等尚未验证 |
| `payment_order_id` | DeepSeek 返回的支付订单号，用于精确匹配支付结果 |
| `qq` | AstrBot 消息发送人的 QQ，由插件绑定到本地订单 |

两个接口的已观察请求没有显式 DeepSeek `user_id` 参数；账户由认证会话关联。插件保存的内部账户命名空间不是上游用户 ID，也不需要管理员手填。

```text
开放平台登录令牌 → DeepSeek 收款账户
发起人的 QQ → 本地订单 → DeepSeek payment_order_id
```

实际扫码人可能是代付者，接口不能证明其 QQ 身份。订单查询权限和成功通知始终归属于本地记录的发起人；充值款项进入创建订单的 DeepSeek 账户。

## 3. 创建支付订单

### 请求

```http
POST https://platform.deepseek.com/api/v1/payments
Authorization: Bearer <WEB_TOKEN>
Content-Type: application/json
```

支付宝示例：

```json
{
  "order_info": {
    "payment_method_type": "CMB_UNIONPAY",
    "fallback_method_type": "ALIPAY",
    "amount": "1",
    "currency": "CNY",
    "request_id": "<CLIENT_REQUEST_UUID>"
  }
}
```

| 字段 | 当前插件规则 |
| --- | --- |
| `payment_method_type` | 固定 `CMB_UNIONPAY` |
| `fallback_method_type` | `/微信喂白饭` 使用 `WECHAT`；`/支付宝喂白饭` 使用 `ALIPAY` |
| `amount` | 用户输入金额，限制为 1～100000 元的整数；插件规范化成两位小数字符串传输，例如整数 1 元发送为 `"1.00"`，不允许充值非整数金额 |
| `currency` | 固定 `CNY` |
| `request_id` | 每次新建订单自动生成，无需管理员或用户提供 |

本次验证中，`shumei_device_id`、`x-device-id` 和 Cookie 可省略，当前插件不提供这些配置或发送它们。插件自动添加的常规网页请求头不需要管理员配置。最少字段组合的真实验证来自支付宝，微信仍需单独验证。

### 成功响应

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "code": 0,
      "payment_order_id": "<PAYMENT_ORDER_ID>",
      "fallback": false,
      "amount": "1",
      "url": "https://qr.95516.com/<PAYMENT_PATH>"
    }
  }
}
```

同时检查 HTTP 状态、外层 `code`、`data.biz_code`、内层 `code`；成功样本均为 0。再验证订单号非空、金额一致和支付链接符合允许范围。

解析路径：

```python
payment = payload["data"]["biz_data"]
order_id = payment["payment_order_id"]
payment_url = payment["url"]
```

`url` 是二维码内容，不是二维码图片地址。插件在内存中生成 PNG 后发送，不抓取网页图片。当前仅接受 `https://qr.95516.com/...` 的返回链接。

主渠道加备用渠道的参数不等于强制限制扫码 App。插件保存用户选择的方式，并在查单时单独记录实际 `payment_channel`；若用户换了扫码方式，只要同一订单的支付成功且核验通过，仍应确认成功。

创建响应未返回二维码到期时间。15 分钟是基于用户实际使用反馈采用的本地有效期。

## 4. 查询订单列表

### 请求

```http
GET https://platform.deepseek.com/auth-api/v0/users/get_all_invoice
Authorization: Bearer <WEB_TOKEN>
Referer: https://platform.deepseek.com/transactions
x-client-platform: web
x-client-version: 1.0.0
```

这是实际成功查询采用的请求头组合，未带 Cookie。已观察请求无查询参数和正文。虽然路径名为 `get_all_invoice`，响应包含充值订单 `payment_orders` 和赠送记录 `bonus_orders`。

### 已支付响应示例

以下保留真实 1 元支付验证中的状态与支付时间，订单号等其他信息已脱敏或省略：

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "invoices": {
        "payment_orders": [
          {
            "payment_order_id": "<PAYMENT_ORDER_ID>",
            "amount": "1",
            "currency": "CNY",
            "payment_order_status": "SUCCESS",
            "paid_at": "2026-10-23T11:21:17+00:00",
            "payment_channel": "ALIPAY"
          }
        ],
        "bonus_orders": []
      }
    }
  }
}
```

`paid_at` 示例对应北京时间 2026-10-23 19:21:17。原始订单还可能包含 `inserted_at` 和 `updated_at`。

```python
orders = payload["data"]["biz_data"]["invoices"]["payment_orders"]
target = next(
    (order for order in orders if order["payment_order_id"] == stored_order_id),
    None,
)
```

只有精确关联到本地订单，且金额、CNY 币种、`SUCCESS` 状态、有效的带时区支付时间均符合要求，才标记 `PAID`。不使用总余额变化、相同金额或付款截图替代订单核对；`bonus_orders` 不属于用户付款，忽略该列表。

## 5. 支付状态

| 上游结果 | 插件处理 |
| --- | --- |
| `CREATED` | 内部为未支付；15 分钟内使用 PENDING，过期使用 EXPIRED |
| `FAILED` | 内部为 FAILED，保留订单并在有限窗口内补查 |
| `SUCCESS` | 核对订单、金额、币种、支付时间；通过后标记 PAID |
| 未知状态或字段不符 | REVIEW_REQUIRED，停止自动确认，需人工核对 |
| 列表中没找到订单 | 不认定支付失败，按有限计划补查 |
| 网络或业务请求失败 | 保留订单状态，退避重试；HTTP 401/403 暂停网络查询并记录管理员提示 |

用户已观察到页面显示“已取消”，但接口仍为 `CREATED`。本地有效期、页面展示与上游状态分开处理，不能将三者视为同一个字段。

本地还包括 CREATING（创建中）状态。创建结果不明或进程在创建期间中断时，转为 REVIEW_REQUIRED，不自动重发 POST。PAID 不会被后续旧结果覆盖。

## 6. 当前插件处理流程

```text
/微信喂白饭 20元 或 /支付宝喂白饭 20元
→ 验证金额、开放群与消息来源
→ 保存本地订单，绑定 QQ、原群会话和所选渠道
→ 创建 DeepSeek 支付订单
→ 保存上游订单号和支付链接
→ @申请人并发送二维码一次
→ 后台共享轮询订单列表
→ 按上游订单号精确匹配并核验支付成功
→ 持久化 PAID 状态
→ 在原群 @申请人通知 DeepSeek 充值成功
```

SQLite `orders` 表中的主要字段：

| 字段 | 用途 |
| --- | --- |
| `id` / `source` | 本地订单号、唯一消息来源键 |
| `account` / `qq` / `origin` | 内部账户命名空间、申请人 QQ、原群会话 |
| `amount` / `request_id` | 人民币金额、创建请求 UUID |
| `upstream_id` / `url` | DeepSeek 订单号和支付链接 |
| `channel` / `actual_channel` | 用户选择方式与上游返回的实际渠道 |
| `state` / `upstream_status` / `paid_at` / `note` | 本地状态、上游状态、支付时间及异常说明 |
| `created` / `next_check` / `polling_done` | 创建时间与有限轮询控制 |
| `notified` / `notify_attempts` / `notify_after` | 成功通知及有限重试控制 |

自动查询在创建后 0～15 分钟每 10 秒执行，到期后在第 16、20、30 分钟补查，然后停止。多个订单共享列表请求；无到期订单不请求接口。网络失败采用退避，登录认证失效暂停查询，重启可恢复未完成记录。

自动查询结束后，用户仍可通过 `/喂白饭查询` 主动核对自己的订单。主动查询有用户冷却与全局请求间隔，不会重启无限轮询。

成功通知之外不催付、不发送取消或过期群消息。通知失败最多尝试三次，间隔 60 秒；通知失败不改变已确认的支付状态。若进程恰好在 QQ 消息已发送、通知标记未保存之间中断，恢复后可能重复通知。

## 7. 本人账单与边界

- `/喂白饭账单` 和 `/喂白饭查询` 仅能访问当前 QQ 发起、已保存到本地的订单。即使知道他人的本地订单号，也不能查询。
- 未与本地申请关联的上游历史订单不展示、不推送给任何群友。
- 创建成功不代表付款成功；二维码过期也不能证明绝对没有付款，需保留有限补查和主动核对入口。
- 创建请求超时可能已生成订单。`request_id` 的服务端幂等性与丢失响应后的恢复能力未确认，不自动重发创建请求。
- 接口分页、保留窗口、限流、退款状态和凭证续期规则仍需验证。
- 当前采用单 DeepSeek 账户，不自动识别上游用户 ID；不能把另一个账户的令牌直接替换到同一数据目录中。
- 金额规则为 1～100000 元整数；真实支付验证覆盖 1 元支付宝，不代表整个金额区间和所有支付方式都已完成实测。
- 不保存真实认证凭证到文档或源码；上线前需完成 AstrBot + NapCat 联调。
