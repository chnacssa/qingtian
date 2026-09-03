"""xihe 自定义异常"""


class ProcessError(Exception):
    """子进程相关异常基类"""
    pass


class ProcessNotFoundError(ProcessError):
    """指定的子进程不存在"""
    pass


class ResourceExhaustedError(ProcessError):
    """资源耗尽（进程数上限、内存上限等）"""
    pass


class SkillRunnerError(ProcessError):
    """子进程内部错误"""
    pass
