from __future__ import annotations
import typing
import asyncio
import traceback

import datetime

from libs.wecom_customer_service_api.api import WecomCSClient
from pkg.platform.adapter import MessagePlatformAdapter
from pkg.platform.types import events as platform_events, message as platform_message
from libs.wecom_customer_service_api.wecomcsevent import WecomCSEvent
from pkg.core import app
from .. import adapter
from ..types import entities as platform_entities
from ...command.errors import ParamNotEnoughError


class WecomMessageConverter(adapter.MessageConverter):
    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain, bot: WecomCSClient):
        content_list = []

        for msg in message_chain:
            if type(msg) is platform_message.Plain:
                content_list.append(
                    {
                        'type': 'text',
                        'content': msg.text,
                    }
                )
            elif type(msg) is platform_message.Image:
                content_list.append(
                    {
                        'type': 'image',
                        'media_id': await bot.get_media_id(msg),
                    }
                )
            elif type(msg) is platform_message.Forward:
                for node in msg.node_list:
                    content_list.extend((await WecomMessageConverter.yiri2target(node.message_chain, bot)))
            else:
                content_list.append(
                    {
                        'type': 'text',
                        'content': str(msg),
                    }
                )

        return content_list

    @staticmethod
    async def target2yiri(message: str, message_id: int = -1):
        yiri_msg_list = []
        yiri_msg_list.append(platform_message.Source(id=message_id, time=datetime.datetime.now()))

        yiri_msg_list.append(platform_message.Plain(text=message))
        chain = platform_message.MessageChain(yiri_msg_list)

        return chain

    @staticmethod
    async def target2yiri_image(picurl: str, message_id: int = -1):
        yiri_msg_list = []
        yiri_msg_list.append(platform_message.Source(id=message_id, time=datetime.datetime.now()))
        yiri_msg_list.append(platform_message.Image(base64=picurl))
        chain = platform_message.MessageChain(yiri_msg_list)

        return chain


class WecomEventConverter:
    @staticmethod
    async def yiri2target(event: platform_events.Event, bot_account_id: int, bot: WecomCSClient) -> WecomCSEvent:
        # only for extracting user information

        if type(event) is platform_events.GroupMessage:
            pass

        if type(event) is platform_events.FriendMessage:
            return event.source_platform_object

    @staticmethod
    async def target2yiri(event: WecomCSEvent):
        """
        将 WecomEvent 转换为平台的 FriendMessage 对象。

        Args:
            event (WecomEvent): 企业微信客服事件。

        Returns:
            platform_events.FriendMessage: 转换后的 FriendMessage 对象。
        """
        # 转换消息链
        if event.type == 'text':
            yiri_chain = await WecomMessageConverter.target2yiri(event.message, event.message_id)
            friend = platform_entities.Friend(
                id=f'u{event.user_id}',
                nickname=str(event.user_id),
                remark='',
            )

            return platform_events.FriendMessage(
                sender=friend, message_chain=yiri_chain, time=event.timestamp, source_platform_object=event
            )
        elif event.type == 'image':
            friend = platform_entities.Friend(
                id=f'u{event.user_id}',
                nickname=str(event.user_id),
                remark='',
            )

            yiri_chain = await WecomMessageConverter.target2yiri_image(picurl=event.picurl, message_id=event.message_id)

            return platform_events.FriendMessage(
                sender=friend, message_chain=yiri_chain, time=event.timestamp, source_platform_object=event
            )


class WecomCSAdapter(adapter.MessagePlatformAdapter):
    bot: WecomCSClient
    ap: app.Application
    bot_account_id: str
    message_converter: WecomMessageConverter = WecomMessageConverter()
    event_converter: WecomEventConverter = WecomEventConverter()
    config: dict

    def __init__(self, config: dict, ap: app.Application):
        self.config = config

        self.ap = ap

        required_keys = [
            'corpid',
            'secret',
            'token',
            'EncodingAESKey',
        ]
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            raise ParamNotEnoughError('企业微信客服缺少相关配置项，请查看文档或联系管理员')

        self.bot = WecomCSClient(
            corpid=config['corpid'],
            secret=config['secret'],
            token=config['token'],
            EncodingAESKey=config['EncodingAESKey'],
        )

    async def reply_message(
        self,
        message_source: platform_events.MessageEvent,
        message: platform_message.MessageChain,
        quote_origin: bool = False,
    ):
        Wecom_event = await WecomEventConverter.yiri2target(message_source, self.bot_account_id, self.bot)
        content_list = await WecomMessageConverter.yiri2target(message, self.bot)

        for content in content_list:
            if content['type'] == 'text':
                await self.bot.send_text_msg(
                    open_kfid=Wecom_event.receiver_id,
                    external_userid=Wecom_event.user_id,
                    msgid=Wecom_event.message_id,
                    content=content['content'],
                )

    async def send_message(self, target_type: str, target_id: str, message: platform_message.MessageChain):
        pass

    def register_listener(
        self,
        event_type: typing.Type[platform_events.Event],
        callback: typing.Callable[[platform_events.Event, adapter.MessagePlatformAdapter], None],
    ):
        async def on_message(event: WecomCSEvent):
            self.bot_account_id = event.receiver_id
            try:
                return await callback(await self.event_converter.target2yiri(event), self)
            except:
                traceback.print_exc()

        if event_type == platform_events.FriendMessage:
            self.bot.on_message('text')(on_message)
            self.bot.on_message('image')(on_message)
        elif event_type == platform_events.GroupMessage:
            pass

    async def run_async(self):
        async def shutdown_trigger_placeholder():
            while True:
                await asyncio.sleep(1)

        await self.bot.run_task(
            host='0.0.0.0',
            port=self.config['port'],
            shutdown_trigger=shutdown_trigger_placeholder,
        )

    async def kill(self) -> bool:
        return False

    async def unregister_listener(
        self,
        event_type: type,
        callback: typing.Callable[[platform_events.Event, MessagePlatformAdapter], None],
    ):
        return super().unregister_listener(event_type, callback)
