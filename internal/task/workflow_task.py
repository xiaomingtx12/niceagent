"""工作流相关的Celery异步任务"""
from uuid import UUID

from celery import shared_task

from internal.extension.logging_extension import logger


@shared_task
def auto_generate_workflow(
    name: str,
    description: str,
    requirements: str,
    account_id: str,  # 改为字符串类型
    node_types: list[str] = None,
) -> None:
    """根据需求自动生成工作流"""
    from app.http.module import injector
    from internal.model import Account
    from internal.service.workflow_generator_service import WorkflowGeneratorService
    try:
        # 1. 获取服务
        workflow_generator_service = injector.get(WorkflowGeneratorService)

        # 2. 获取账号对象
        from pkg.sqlalchemy import SQLAlchemy

        db = injector.get(SQLAlchemy)
        # 将字符串转换为UUID
        account = db.session.query(Account).get(UUID(account_id))

        if not account:
            logger.error("账号不存在: %s", account_id)
            return

        # 3. 生成工作流（内部会调用LLM，耗时较长，期间数据库连接可能因空闲被远程服务器断开）
        workflow_generator_service.generate_workflow_from_requirements(
            name=name,
            description=description,
            requirements=requirements,
            account=account,
            node_types=node_types,
        )

        logger.info("工作流生成成功: %s", name)

    except Exception as e:
        logger.exception("工作流生成失败: %s", e)
    finally:
        # 确保session被关闭，归还连接到连接池
        from pkg.sqlalchemy import SQLAlchemy
        db = injector.get(SQLAlchemy)
        db.session.remove()


@shared_task
def auto_generate_app_with_workflow(
    app_name: str,
    app_description: str,
    preset_prompt: str,
    workflow_requirements: str,
    account_id: str,  # 改为字符串类型
    auto_publish: bool = False,
) -> None:
    """自动创建Agent应用并生成关联的工作流"""
    from app.http.module import injector
    from internal.model import Account, App, Workflow
    from internal.model.app import AppConfigVersion
    from internal.service.workflow_generator_service import WorkflowGeneratorService
    from internal.service.workflow_service import WorkflowService
    from internal.entity.app_entity import AppStatus, AppConfigType, DEFAULT_APP_CONFIG
    from internal.entity.workflow_entity import WorkflowStatus
    try:
        # 1. 获取服务
        workflow_generator_service = injector.get(WorkflowGeneratorService)
        workflow_service = injector.get(WorkflowService)

        from pkg.sqlalchemy import SQLAlchemy
        db = injector.get(SQLAlchemy)

        # 2. 先获取账号对象验证存在性
        account = db.session.query(Account).get(UUID(account_id))
        if not account:
            logger.error("账号不存在: %s", account_id)
            return

        # 3. 调用LLM生成工作流配置（耗时30-60秒，期间数据库连接可能因空闲被断开）
        #    这里只调用LLM生成配置，不涉及数据库写入
        workflow_config = workflow_generator_service.generate_workflow_config_with_llm(
            name=f"{app_name}_workflow",
            description=f"{app_name}的配套工作流",
            requirements=workflow_requirements,
            account=account,
        )

        # 4. LLM调用完成后，关闭旧session释放可能已断开的连接
        #    pool_pre_ping会在下次获取连接时自动检测并重连
        db.session.remove()

        # 5. 重新获取account（使用新鲜的数据库连接）
        account = db.session.query(Account).get(UUID(account_id))
        if not account:
            logger.error("账号不存在: %s", account_id)
            return

        # 6. 校验并修复LLM生成的配置
        validated_config = workflow_generator_service._validate_and_fix_workflow_config(
            workflow_config, account
        )

        # 7. 生成唯一的工具调用名称
        workflow_name = f"{app_name}_workflow"
        tool_call_name = workflow_generator_service._generate_unique_tool_call_name(
            workflow_name, account
        )

        # 8. 创建工作流记录
        from internal.entity.workflow_entity import DEFAULT_WORKFLOW_CONFIG
        workflow = workflow_generator_service.create(
            Workflow,
            **{
                "account_id": account.id,
                "name": workflow_name,
                "description": f"{app_name}的配套工作流",
                "tool_call_name": tool_call_name,
                "status": WorkflowStatus.DRAFT,
                "is_debug_passed": False,
                **DEFAULT_WORKFLOW_CONFIG,
                "draft_graph": {
                    "nodes": validated_config["nodes"],
                    "edges": validated_config["edges"],
                },
            },
        )

        # 9. 创建App和AppConfigVersion
        default_icon = "https://imooc-llmops-1304251364.cos.ap-guangzhou.myqcloud.com/2024/12/06/agent-icon.png"

        with db.auto_commit():
            app = App(
                account_id=account.id,
                name=app_name,
                icon=default_icon,
                description=app_description,
                status=AppStatus.DRAFT,
            )
            db.session.add(app)
            db.session.flush()

            app_config_version = AppConfigVersion(
                app_id=app.id,
                version=0,
                config_type=AppConfigType.DRAFT,
                **{
                    **DEFAULT_APP_CONFIG,
                    "preset_prompt": preset_prompt,
                    "workflows": [str(workflow.id)],
                },
            )
            db.session.add(app_config_version)
            db.session.flush()

            app.draft_app_config_id = app_config_version.id

        # 10. 如果需要自动发布
        if auto_publish:
            workflow_service.update(workflow, is_debug_passed=True)
            workflow_service.publish_workflow(workflow.id, account)
            from internal.service.app_service import AppService
            app_service = injector.get(AppService)
            app_service.publish_draft_app_config(app.id, account)

        logger.info("应用和工作流生成成功: %s", app_name)

    except Exception as e:
        logger.exception("应用和工作流生成失败: %s", e)
    finally:
        # 确保session被关闭，归还连接到连接池
        from pkg.sqlalchemy import SQLAlchemy
        db = injector.get(SQLAlchemy)
        db.session.remove()
