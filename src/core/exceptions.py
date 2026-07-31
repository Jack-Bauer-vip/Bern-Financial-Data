"""Bern Financial Data 自定义异常层次"""


class BernError(Exception):
    """所有模块异常的基类"""
    pass


class NetworkError(BernError):
    """网络请求相关错误（超时、连接失败等）"""
    pass


class DataFetchError(BernError):
    """数据源 API 返回错误或数据异常"""
    pass


class RateLimitError(BernError):
    """API 频率限制/请求超限"""
    pass


class DataParsingError(BernError):
    """数据解析/格式转换失败"""
    pass


class DatabaseError(BernError):
    """数据库操作失败"""
    pass
