from __future__ import annotations

import asyncio
import traceback

from ..core import app, entities


class Controller:
    """总控制器"""

    ap: app.Application

    semaphore: asyncio.Semaphore = None
    """请求并发控制信号量"""

    def __init__(self, ap: app.Application):
        self.ap = ap
        self.semaphore = asyncio.Semaphore(self.ap.instance_config.data['concurrency']['pipeline'])

    async def consumer(self):
        """事件处理循环"""
        try:
            while True:
                selected_query: entities.Query = None

                # 取请求
                async with self.ap.query_pool:
                    queries: list[entities.Query] = self.ap.query_pool.queries

                    for query in queries:
                        session = await self.ap.sess_mgr.get_session(query)
                        self.ap.logger.debug(f'Checking query {query} session {session}')

                        if not session.semaphore.locked():
                            selected_query = query
                            await session.semaphore.acquire()

                            break

                    if selected_query:  # 找到了
                        queries.remove(selected_query)
                    else:  # 没找到 说明：没有请求 或者 所有query对应的session都已达到并发上限
                        await self.ap.query_pool.condition.wait()
                        continue

                if selected_query:

                    async def _process_query(selected_query: entities.Query):
                        async with self.semaphore:  # 总并发上限
                            # find pipeline
                            # Here firstly find the bot, then find the pipeline, in case the bot adapter's config is not the latest one.
                            # Like aiocqhttp, once a client is connected, even the adapter was updated and restarted, the existing client connection will not be affected.
                            bot = await self.ap.platform_mgr.get_bot_by_uuid(selected_query.bot_uuid)
                            if bot:
                                pipeline = await self.ap.pipeline_mgr.get_pipeline_by_uuid(
                                    bot.bot_entity.use_pipeline_uuid
                                )
                                if pipeline:
                                    await pipeline.run(selected_query)

                        async with self.ap.query_pool:
                            (await self.ap.sess_mgr.get_session(selected_query)).semaphore.release()
                            # 通知其他协程，有新的请求可以处理了
                            self.ap.query_pool.condition.notify_all()

                    self.ap.task_mgr.create_task(
                        _process_query(selected_query),
                        kind='query',
                        name=f'query-{selected_query.query_id}',
                        scopes=[
                            entities.LifecycleControlScope.APPLICATION,
                            entities.LifecycleControlScope.PLATFORM,
                        ],
                    )

        except Exception as e:
            # traceback.print_exc()
            self.ap.logger.error(f'控制器循环出错: {e}')
            self.ap.logger.error(f'Traceback: {traceback.format_exc()}')

    async def run(self):
        """运行控制器"""
        await self.consumer()
