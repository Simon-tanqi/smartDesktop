@log_execution()
def path_params(*params):
    """装饰器：标记函数的哪些参数是路径"""
    def decorator(func):
        func._path_params = list(params)
        return func
    return decorator
