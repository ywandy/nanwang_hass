"""南网在线集成常量。"""

from __future__ import annotations

DOMAIN = "nanwang"
MANUFACTURER = "中国南方电网"

CONF_PHONE = "phone"
CONF_CODE = "code"
CONF_TOKEN = "token"
CONF_CUSTNUMBER = "custnumber"
CONF_COOKIES = "cookies"
CONF_AREA_CODE = "area_code"
CONF_AUTH_METHOD = "auth_method"

AUTH_SMS = "sms"
AUTH_TOKEN = "token"

# 登录/余额查询用的地区编码（前端 defaultAreaCode）
AREA_CODES: dict[str, str] = {
    "080000": "广州",
    "030000": "广东",
    "040000": "广西",
    "050000": "云南",
    "060000": "贵州",
    "070000": "海南",
    "090000": "深圳",
}
DEFAULT_AREA_CODE = "030000"

# queryBindEleUsers.eleType，只收录真实账号确认过的
ELE_TYPES: dict[str, str] = {"100": "充电桩", "500": "居民"}

# 固定在每天这几个整点刷新（当地时间）
REFRESH_HOURS = (0, 12)

# 每次刷新都重新同步最近这么多天的逐日数据（跨月会连上个月一起取）
RECENT_DAYS = 15

# 逐日电量导入长期统计：首次回填月数 / 断档后最多补的月数
STATS_INITIAL_MONTHS = 6
STATS_MAX_MONTHS = 12
