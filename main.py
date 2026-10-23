import asyncio
import hashlib
import io
import json
import time

import qrcode
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .api import AuthenticationError, DeepSeekPayments, PaymentError, money
from .service import Service
from .store import CHANNELS, LABELS, Store


class DsAutoPay(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # Keep legacy namespaces readable; new installs have one internal account.
        self.account = str(config.get("account_ref", "default"))
        self.store = Store(
            StarTools.get_data_dir("astrbot_plugin_dsAutoPay") / "orders.sqlite3"
        )
        self.api = DeepSeekPayments(config.get("web_token", ""))
        self.service = Service(self.store, self.api, self.account)
        self.worker = None
        self.active = set()
        self.stopping = False

    async def initialize(self):
        self.store.recover()
        self.worker = asyncio.create_task(self.poll_loop())

    async def terminate(self):
        self.stopping = True
        tasks = list(self.active)
        if self.worker:
            tasks.append(self.worker)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.api.close()

    def ready(self):
        return bool(self.config.get("web_token", "").strip())

    def guard(self, event, recharge=False):
        event.stop_event()
        if self.stopping:
            raise ValueError("插件正在重载，请稍后再试")
        if event.get_platform_name() != "aiocqhttp":
            raise ValueError("仅支持 NapCat / OneBot QQ")
        group = str(event.get_group_id() or "")
        allowed = {str(g) for g in self.config.get("allowed_groups", [])}
        if (group and group not in allowed) or (recharge and not group):
            raise ValueError("请在已开放的 QQ 群内发起充值")
        qq = str(event.get_sender_id())
        if not qq.isascii() or not qq.isdigit():
            raise ValueError("无法识别 QQ 用户")
        return qq

    def source(self, event):
        mid = getattr(event.message_obj, "message_id", None)
        if mid is None or str(mid) == "":
            raise ValueError("消息缺少唯一编号，无法安全创建订单")
        key = [
            self.account,
            event.get_self_id(),
            event.unified_msg_origin,
            event.get_sender_id(),
            str(mid),
        ]
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()

    @staticmethod
    def summary(row):
        return (
            f"订单：{row['id']}\n金额：{row['amount']} 元\n选择方式：{CHANNELS[row['channel']]}\n状态：{LABELS[row['state']]}"
            + (
                f"\n实际渠道：{CHANNELS.get(row['actual_channel'], '其他')}"
                if row.get("actual_channel")
                else ""
            )
        )

    @filter.command("喂白饭")
    async def choose_payment(self, event: AstrMessageEvent):
        """显示微信与支付宝喂白饭入口，不创建订单。"""
        try:
            self.guard(event, recharge=True)
            yield event.plain_result(
                "喂白饭，请输入 1 元至 100000 元之间的整数金额。请选择支付方式：\n/微信喂白饭 20元\n/支付宝喂白饭 20元\n款项进入运营方的 DeepSeek 账户。"
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.command("微信喂白饭")
    async def wechat_recharge(self, event: AstrMessageEvent, amount: str = ""):
        """使用微信充值：/微信喂白饭 20元。"""
        async for result in self.recharge(event, amount, "WECHAT"):
            yield result

    @filter.command("支付宝喂白饭")
    async def alipay_recharge(self, event: AstrMessageEvent, amount: str = ""):
        """使用支付宝喂白饭：/支付宝喂白饭 20元。"""
        async for result in self.recharge(event, amount, "ALIPAY"):
            yield result

    async def recharge(self, event: AstrMessageEvent, amount: str, channel: str):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            qq = self.guard(event, recharge=True)
            if not amount:
                command = "微信喂白饭" if channel == "WECHAT" else "支付宝喂白饭"
                raise ValueError(
                    f"用法：/{command} 20元；款项充值到运营方的 DeepSeek 账户"
                )
            if not self.ready():
                raise ValueError("管理员尚未配置支付服务")
            amount = money(amount.removesuffix("元"))
            row, fresh = await self.service.create(
                qq, event.unified_msg_origin, self.source(event), amount, channel
            )
            if not fresh:
                yield event.plain_result(
                    "已有订单，不会重复创建或发码。\n"
                    + self.summary(row)
                    + "\n可用 /喂白饭查询 订单号 查询。"
                )
                return
            buffer = io.BytesIO()
            qrcode.make(row["url"]).save(buffer, format="PNG")
            yield event.chain_result(
                [
                    At(qq=qq),
                    Plain(
                        f"\n给 DeepSeek 喂白饭 {row['amount']} 元\n订单：{row['id']}\n请在 15 分钟内完成支付。款项进入运营方的 DeepSeek 账户。\n请使用{CHANNELS[row['channel']]}扫码。"
                    ),
                    Image.fromBytes(buffer.getvalue()),
                ]
            )
        except (ValueError, PaymentError) as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:  # noqa: BLE001 -- do not disclose upstream/session data
            logger.warning("dsAutoPay 充值处理异常 (%s)", type(exc).__name__)
            yield event.plain_result(
                "处理异常，请使用 /喂白饭账单 查看是否已创建订单，勿立即重复充值。"
            )
        finally:
            self.active.discard(task)

    @filter.command("喂白饭账单")
    async def bills(self, event: AstrMessageEvent, page: int = 1):
        """只查看本人在本插件创建的订单，每页五条。"""
        try:
            qq = self.guard(event)
            rows = self.store.history(self.account, qq, page)
            text = "\n\n".join(self.summary(row) for row in rows) or "暂无本人充值记录"
            yield event.plain_result(text)
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.command("喂白饭查询")
    async def query(self, event: AstrMessageEvent, order_id: str = ""):
        """查询本人的订单；省略订单号查询最近一笔。"""
        task = asyncio.current_task()
        self.active.add(task)
        try:
            qq = self.guard(event)
            if not order_id:
                rows = self.store.history(self.account, qq)
                if not rows:
                    raise ValueError("暂无本人充值记录")
                order_id = rows[0]["id"]
            if not self.ready():
                row = self.store.own(self.account, qq, order_id)
            else:
                row = await self.service.check(qq, order_id)
            yield event.plain_result(
                "最近确认的结果（查询有冷却间隔）：\n" + self.summary(row)
            )
        except (ValueError, PaymentError) as exc:
            yield event.plain_result(str(exc))
        finally:
            self.active.discard(task)

    async def notify_paid(self):
        for row in self.store.notifications(self.account, time.time()):
            self.store.notification_attempt(row["id"], time.time())
            try:
                sent = await asyncio.wait_for(
                    self.context.send_message(
                        row["origin"],
                        MessageChain(
                            [
                                At(qq=row["qq"]),
                                Plain(
                                    f"\n喂白饭成功，已确认 DeepSeek 到账：{row['amount']} 元。\n订单：{row['id']}"
                                ),
                            ]
                        ),
                    ),
                    timeout=20,
                )
                if sent is False:
                    raise RuntimeError("未找到原群平台")
                self.store.notified(row["id"])
            except Exception as exc:  # noqa: BLE001 -- payment is final even if QQ send fails
                logger.warning("dsAutoPay 到账通知发送失败 (%s)", type(exc).__name__)

    async def poll_loop(self):
        while True:
            try:
                if self.ready():
                    await self.service.tick()
                await self.notify_paid()
            except AuthenticationError:
                logger.warning(
                    "dsAutoPay 登录认证失败，已暂停网络查询；请更新凭证并重载插件"
                )
            except Exception as exc:  # noqa: BLE001 -- keep worker alive; service handles backoff
                logger.warning("dsAutoPay 后台处理失败 (%s)", type(exc).__name__)
            await asyncio.sleep(2)
