import quart

from .. import group
from .....utils import constants


@group.group_class('system', '/api/v1/system')
class SystemRouterGroup(group.RouterGroup):
    async def initialize(self) -> None:
        @self.route('/info', methods=['GET'], auth_type=group.AuthType.NONE)
        async def _() -> str:
            return self.success(
                data={
                    'version': constants.semantic_version,
                    'debug': constants.debug_mode,
                    'enabled_platform_count': len(self.ap.platform_mgr.get_running_adapters()),
                }
            )

        @self.route('/tasks', methods=['GET'], auth_type=group.AuthType.USER_TOKEN)
        async def _() -> str:
            task_type = quart.request.args.get('type')

            if task_type == '':
                task_type = None

            return self.success(data=self.ap.task_mgr.get_tasks_dict(task_type))

        @self.route('/tasks/<task_id>', methods=['GET'], auth_type=group.AuthType.USER_TOKEN)
        async def _(task_id: str) -> str:
            task = self.ap.task_mgr.get_task_by_id(int(task_id))

            if task is None:
                return self.http_status(404, 404, 'Task not found')

            return self.success(data=task.to_dict())

        @self.route('/reload', methods=['POST'], auth_type=group.AuthType.USER_TOKEN)
        async def _() -> str:
            json_data = await quart.request.json

            scope = json_data.get('scope')

            await self.ap.reload(scope=scope)
            return self.success()

        @self.route('/_debug/exec', methods=['POST'], auth_type=group.AuthType.USER_TOKEN)
        async def _() -> str:
            if not constants.debug_mode:
                return self.http_status(403, 403, 'Forbidden')

            py_code = await quart.request.data

            ap = self.ap

            return self.success(data=exec(py_code, {'ap': ap}))
