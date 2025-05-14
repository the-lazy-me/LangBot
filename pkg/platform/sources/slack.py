from __future__ import annotations
import typing
import asyncio
import traceback

import datetime

from libs.slack_api.api import SlackClient
from pkg.platform.adapter import MessagePlatformAdapter
from pkg.platform.types import events as platform_events, message as platform_message
from libs.slack_api.slackevent import SlackEvent
from pkg.core import app
from .. import adapter
from ..types import entities as platform_entities
from ...command.errors import ParamNotEnoughError
from ...utils import image


class SlackMessageConverter(adapter.MessageConverter):
    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain):
        content_list = []
        for msg in message_chain:
            if type(msg) is platform_message.Plain:
                content_list.append(
                    {
                        'content': msg.text,
                    }
                )

        return content_list

    @staticmethod
    async def target2yiri(message: str, message_id: str, pic_url: str, bot: SlackClient):
        yiri_msg_list = []
        yiri_msg_list.append(platform_message.Source(id=message_id, time=datetime.datetime.now()))
        if pic_url is not None:
            base64_url = await image.get_slack_image_to_base64(pic_url=pic_url, bot_token=bot.bot_token)
            yiri_msg_list.append(platform_message.Image(base64=base64_url))

        yiri_msg_list.append(platform_message.Plain(text=message))
        chain = platform_message.MessageChain(yiri_msg_list)
        return chain


class SlackEventConverter(adapter.EventConverter):
    @staticmethod
    async def yiri2target(event: platform_events.MessageEvent) -> SlackEvent:
        return event.source_platform_object

    @staticmethod
    async def target2yiri(event: SlackEvent, bot: SlackClient):
        yiri_chain = await SlackMessageConverter.target2yiri(
            message=event.text, message_id=event.message_id, pic_url=event.pic_url, bot=bot
        )

        if event.type == 'channel':
            yiri_chain.insert(0, platform_message.At(target='SlackBot'))

            sender = platform_entities.GroupMember(
                id=event.user_id,
                member_name=str(event.sender_name),
                permission='MEMBER',
                group=platform_entities.Group(
                    id=event.channel_id, name='MEMBER', permission=platform_entities.Permission.Member
                ),
                special_title='',
                join_timestamp=0,
                last_speak_timestamp=0,
                mute_time_remaining=0,
            )
            time = int(datetime.datetime.utcnow().timestamp())
            return platform_events.GroupMessage(
                sender=sender, message_chain=yiri_chain, time=time, source_platform_object=event
            )

        if event.type == 'im':
            return platform_events.FriendMessage(
                sender=platform_entities.Friend(id=event.user_id, nickname=event.sender_name, remark=''),
                message_chain=yiri_chain,
                time=float(datetime.datetime.now().timestamp()),
                source_platform_object=event,
            )


class SlackAdapter(adapter.MessagePlatformAdapter):
    bot: SlackClient
    ap: app.Application
    bot_account_id: str
    message_converter: SlackMessageConverter = SlackMessageConverter()
    event_converter: SlackEventConverter = SlackEventConverter()
    config: dict

    def __init__(self, config: dict, ap: app.Application):
        self.config = config
        self.ap = ap
        required_keys = [
            'bot_token',
            'signing_secret',
        ]
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            raise ParamNotEnoughError('Slack机器人缺少相关配置项，请查看文档或联系管理员')

        self.bot = SlackClient(bot_token=self.config['bot_token'], signing_secret=self.config['signing_secret'])

    async def reply_message(
        self,
        message_source: platform_events.MessageEvent,
        message: platform_message.MessageChain,
        quote_origin: bool = False,
    ):
        slack_event = await SlackEventConverter.yiri2target(message_source)

        content_list = await SlackMessageConverter.yiri2target(message)

        for content in content_list:
            if slack_event.type == 'channel':
                await self.bot.send_message_to_channel(content['content'], slack_event.channel_id)
            if slack_event.type == 'im':
                await self.bot.send_message_to_one(content['content'], slack_event.user_id)

    async def send_message(self, target_type: str, target_id: str, message: platform_message.MessageChain):
        content_list = await SlackMessageConverter.yiri2target(message)
        for content in content_list:
            if target_type == 'person':
                await self.bot.send_message_to_one(content['content'], target_id)
            if target_type == 'group':
                await self.bot.send_message_to_channel(content['content'], target_id)

    def register_listener(
        self,
        event_type: typing.Type[platform_events.Event],
        callback: typing.Callable[[platform_events.Event, adapter.MessagePlatformAdapter], None],
    ):
        async def on_message(event: SlackEvent):
            self.bot_account_id = 'SlackBot'
            try:
                return await callback(await self.event_converter.target2yiri(event, self.bot), self)
            except:
                traceback.print_exc()

        if event_type == platform_events.FriendMessage:
            self.bot.on_message('im')(on_message)
        elif event_type == platform_events.GroupMessage:
            self.bot.on_message('channel')(on_message)

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
