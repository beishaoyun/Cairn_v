"""Cairn Server 业务路由包。

settings.py 由 create_app 显式挂载；其余（engagements/targets/projects/findings/
coverage/capture/progress/report 等）由 20-25 等 Agent 各自新增，模块内暴露名为
``router`` 的 fastapi.APIRouter 实例，app.register_business_routers 自动发现挂载。
"""
