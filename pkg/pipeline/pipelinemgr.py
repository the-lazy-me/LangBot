from __future__ import annotations

import typing
import traceback

import sqlalchemy

from ..core import app, entities
from . import entities as pipeline_entities
from ..entity.persistence import pipeline as persistence_pipeline
from . import stage
from ..platform.types import message as platform_message, events as platform_events
from ..plugin import events
from ..utils import importutil

from . import (
    resprule,
    bansess,
    cntfilter,
    process,
    longtext,
    respback,
    wrapper,
    preproc,
    ratelimit,
    msgtrun,
)

importutil.import_modules_in_pkgs(
    [
        resprule,
        bansess,
        cntfilter,
        process,
        longtext,
        respback,
        wrapper,
        preproc,
        ratelimit,
        msgtrun,
    ]
)


class StageInstContainer:
    """阶段实例容器"""

    inst_name: str

    inst: stage.PipelineStage

    def __init__(self, inst_name: str, inst: stage.PipelineStage):
        self.inst_name = inst_name
        self.inst = inst


class RuntimePipeline:
    """运行时流水线"""

    ap: app.Application

    pipeline_entity: persistence_pipeline.LegacyPipeline
    """流水线实体"""

    stage_containers: list[StageInstContainer]
    """阶段实例容器"""

    def __init__(
        self,
        ap: app.Application,
        pipeline_entity: persistence_pipeline.LegacyPipeline,
        stage_containers: list[StageInstContainer],
    ):
        self.ap = ap
        self.pipeline_entity = pipeline_entity
        self.stage_containers = stage_containers

    async def run(self, query: entities.Query):
        query.pipeline_config = self.pipeline_entity.config
        await self.process_query(query)

    async def _check_output(self, query: entities.Query, result: pipeline_entities.StageProcessResult):
        """检查输出"""
        if result.user_notice:
            # 处理str类型

            if isinstance(result.user_notice, str):
                result.user_notice = platform_message.MessageChain(platform_message.Plain(result.user_notice))
            elif isinstance(result.user_notice, list):
                result.user_notice = platform_message.MessageChain(*result.user_notice)

            if query.pipeline_config['output']['misc']['at-sender'] and isinstance(
                query.message_event, platform_events.GroupMessage
            ):
                result.user_notice.insert(0, platform_message.At(query.message_event.sender.id))

            await query.adapter.reply_message(
                message_source=query.message_event,
                message=result.user_notice,
                quote_origin=query.pipeline_config['output']['misc']['quote-origin'],
            )
        if result.debug_notice:
            self.ap.logger.debug(result.debug_notice)
        if result.console_notice:
            self.ap.logger.info(result.console_notice)
        if result.error_notice:
            self.ap.logger.error(result.error_notice)

    async def _execute_from_stage(
        self,
        stage_index: int,
        query: entities.Query,
    ):
        """从指定阶段开始执行，实现了责任链模式和基于生成器的阶段分叉功能。

        如何看懂这里为什么这么写？
        去问 GPT-4:
            Q1: 现在有一个责任链，其中有多个stage，query对象在其中传递，stage.process可能返回Result也有可能返回typing.AsyncGenerator[Result, None]，
                如果返回的是生成器，需要挨个生成result，检查是否result中是否要求继续，如果要求继续就进行下一个stage。如果此次生成器产生的result处理完了，就继续生成下一个result，
                调用后续的stage，直到该生成器全部生成完。责任链中可能有多个stage会返回生成器
            Q2: 不是这样的，你可能理解有误。如果我们责任链上有这些Stage：

                A B C D E F G

                如果所有的stage都返回Result，且所有Result都要求继续，那么执行顺序是：

                A B C D E F G

                现在假设C返回的是AsyncGenerator，那么执行顺序是：

                A B C D E F G C D E F G C D E F G ...
            Q3: 但是如果不止一个stage会返回生成器呢？
        """
        i = stage_index

        while i < len(self.stage_containers):
            stage_container = self.stage_containers[i]

            query.current_stage = stage_container  # 标记到 Query 对象里

            result = stage_container.inst.process(query, stage_container.inst_name)

            if isinstance(result, typing.Coroutine):
                result = await result

            if isinstance(result, pipeline_entities.StageProcessResult):  # 直接返回结果
                self.ap.logger.debug(f'Stage {stage_container.inst_name} processed query {query} res {result}')
                await self._check_output(query, result)

                if result.result_type == pipeline_entities.ResultType.INTERRUPT:
                    self.ap.logger.debug(f'Stage {stage_container.inst_name} interrupted query {query}')
                    break
                elif result.result_type == pipeline_entities.ResultType.CONTINUE:
                    query = result.new_query
            elif isinstance(result, typing.AsyncGenerator):  # 生成器
                self.ap.logger.debug(f'Stage {stage_container.inst_name} processed query {query} gen')

                async for sub_result in result:
                    self.ap.logger.debug(f'Stage {stage_container.inst_name} processed query {query} res {sub_result}')
                    await self._check_output(query, sub_result)

                    if sub_result.result_type == pipeline_entities.ResultType.INTERRUPT:
                        self.ap.logger.debug(f'Stage {stage_container.inst_name} interrupted query {query}')
                        break
                    elif sub_result.result_type == pipeline_entities.ResultType.CONTINUE:
                        query = sub_result.new_query
                        await self._execute_from_stage(i + 1, query)
                break

            i += 1

    async def process_query(self, query: entities.Query):
        """处理请求"""
        try:
            # ======== 触发 MessageReceived 事件 ========
            event_type = (
                events.PersonMessageReceived
                if query.launcher_type == entities.LauncherTypes.PERSON
                else events.GroupMessageReceived
            )

            event_ctx = await self.ap.plugin_mgr.emit_event(
                event=event_type(
                    launcher_type=query.launcher_type.value,
                    launcher_id=query.launcher_id,
                    sender_id=query.sender_id,
                    message_chain=query.message_chain,
                    query=query,
                )
            )

            if event_ctx.is_prevented_default():
                return

            self.ap.logger.debug(f'Processing query {query}')

            await self._execute_from_stage(0, query)
        except Exception as e:
            inst_name = query.current_stage.inst_name if query.current_stage else 'unknown'
            self.ap.logger.error(f'处理请求时出错 query_id={query.query_id} stage={inst_name} : {e}')
            self.ap.logger.error(f'Traceback: {traceback.format_exc()}')
        finally:
            self.ap.logger.debug(f'Query {query} processed')


class PipelineManager:
    """流水线管理器"""

    # ====== 4.0 ======

    ap: app.Application

    pipelines: list[RuntimePipeline]

    stage_dict: dict[str, type[stage.PipelineStage]]

    def __init__(self, ap: app.Application):
        self.ap = ap
        self.pipelines = []

    async def initialize(self):
        self.stage_dict = {name: cls for name, cls in stage.preregistered_stages.items()}

        await self.load_pipelines_from_db()

    async def load_pipelines_from_db(self):
        self.ap.logger.info('Loading pipelines from db...')

        result = await self.ap.persistence_mgr.execute_async(sqlalchemy.select(persistence_pipeline.LegacyPipeline))

        pipelines = result.all()

        # load pipelines
        for pipeline in pipelines:
            await self.load_pipeline(pipeline)

    async def load_pipeline(
        self,
        pipeline_entity: persistence_pipeline.LegacyPipeline
        | sqlalchemy.Row[persistence_pipeline.LegacyPipeline]
        | dict,
    ):
        if isinstance(pipeline_entity, sqlalchemy.Row):
            pipeline_entity = persistence_pipeline.LegacyPipeline(**pipeline_entity._mapping)
        elif isinstance(pipeline_entity, dict):
            pipeline_entity = persistence_pipeline.LegacyPipeline(**pipeline_entity)

        # initialize stage containers according to pipeline_entity.stages
        stage_containers: list[StageInstContainer] = []
        for stage_name in pipeline_entity.stages:
            stage_containers.append(StageInstContainer(inst_name=stage_name, inst=self.stage_dict[stage_name](self.ap)))

        for stage_container in stage_containers:
            await stage_container.inst.initialize(pipeline_entity.config)

        runtime_pipeline = RuntimePipeline(self.ap, pipeline_entity, stage_containers)
        self.pipelines.append(runtime_pipeline)

    async def get_pipeline_by_uuid(self, uuid: str) -> RuntimePipeline | None:
        for pipeline in self.pipelines:
            if pipeline.pipeline_entity.uuid == uuid:
                return pipeline
        return None

    async def remove_pipeline(self, uuid: str):
        for pipeline in self.pipelines:
            if pipeline.pipeline_entity.uuid == uuid:
                self.pipelines.remove(pipeline)
                return
