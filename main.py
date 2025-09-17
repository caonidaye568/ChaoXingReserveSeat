import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue

# =============== 新增: 关键窗口内存日志缓冲 ===============
import io, sys
class BufferedHandler(logging.Handler):
    def __init__(self, fmt):
        super().__init__()
        self.buf = io.StringIO()
        self.active = False
        self.setFormatter(logging.Formatter(fmt))

    def emit(self, record):
        msg = self.format(record)
        if self.active:
            self.buf.write(msg + "\n")
        else:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    def flush_buffer(self):
        self.buf.seek(0)
        for line in self.buf:
            sys.stdout.write(line)
        sys.stdout.flush()
        self.buf = io.StringIO()

BUF_FMT = "%(asctime)s - %(levelname)s - %(message)s"
buf_handler = BufferedHandler(BUF_FMT)
logging.basicConfig(level=logging.INFO, handlers=[buf_handler])

from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%H:%M:%S")
    if action
    else time.strftime("%H:%M:%S", time.localtime())
)
get_current_dayofweek = lambda action: (
    (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%A")
    if action
    else time.strftime("%A", time.localtime())
)

# ================= 你的原始开关，保留 =================
SLEEPTIME = 0.1
RESERVE_TARGET_TIME = "19:52:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 1
RESERVE_NEXT_DAY = False

CAPTCHA_POOL_SIZE = 5
CAPTCHA_PRELOAD_TIME = 5  # 提前 N 秒启动验证码池

# =============== 新增: 关键窗口静音范围（秒） ===============
QUIET_WINDOW_BEFORE = 1.2
QUIET_WINDOW_AFTER  = 1.0

# =============== 新增: 连接池参数 ===============
POOL_CONNECTIONS = 16
POOL_MAXSIZE = 32

class CaptchaPool:
    """验证码缓存池"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        
    def start_preloading(self):
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        def preload_worker():
            while self.is_active and self.captcha_queue.qsize() < self.pool_size:
                try:
                    captcha = self.session.resolve_captcha()
                    if captcha:
                        self.captcha_queue.put(captcha)
                        logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                    time.sleep(0.5)
                except Exception as e:
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(1)
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def get_captcha(self):
        if not self.captcha_queue.empty():
            return self.captcha_queue.get()
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def stop(self):
        self.is_active = False

# =============== 新增: 调优 requests 连接池/Keep-Alive ===============
def tune_requests_session(sess):
    """放大连接池，稳定长连接，减少握手成本"""
    try:
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=POOL_CONNECTIONS, pool_maxsize=POOL_MAXSIZE, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        # 明示长连接
        sess.headers.update({"Connection": "keep-alive"})
    except Exception as e:
        logging.warning(f"连接池调优失败: {e}")

# =============== 新增: 与服务器时间对齐，返回秒级偏移 ===============
def get_server_time_offset(sess):
    """
    读取 Chaoxing 返回头部 Date，估算本地与服务器的时间偏差（秒）。
    正值表示本地时钟比服务器慢，需要更早醒来；负值反之。
    """
    try:
        # 用一个稳定页面拿 Date 头，避免重定向迷惑
        resp = sess.requests.get("https://office.chaoxing.com/data/apps/seat/getusedtimes", timeout=3, verify=False)
        date_str = resp.headers.get("Date")
        if not date_str:
            return 0.0
        # 示例: 'Wed, 17 Sep 2025 11:35:02 GMT'
        server_dt = datetime.datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT")
        server_dt = server_dt.replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        local_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        offset = (local_dt - server_dt).total_seconds()
        logging.info(f"⏱️ 服务器时间偏移估计: {offset:+.3f}s（本地-服务器）")
        return offset
    except Exception as e:
        logging.warning(f"获取服务器时间失败，使用本地时间: {e}")
        return 0.0

def warm_up_session(session, roomid, seatid):
    """Session预热 - 模拟正常浏览行为"""
    try:
        logging.info("🔥 开始Session预热...")
        session.requests.get(
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}",
            verify=False
        )
        time.sleep(0.5)
        session.requests.get("https://office.chaoxing.com/data/apps/seat/getusedtimes", verify=False)
        time.sleep(0.5)
        logging.info("✅ Session预热完成")
    except Exception as e:
        logging.warning(f"⚠️ Session预热失败: {e}")

# =============== 新增: 提交前 JIT 刷页，立刻取 token，立刻提交，失败立刻复试一次 ===============
def rapid_submit_single(session, times, roomid, seatid, captcha_pool, action):
    start_time = time.time()
    try:
        # 1) 提交前轻刷座位页，刷新 cookie/风控状态，尽量避免“页面停留过久”
        try:
            session.requests.get(
                f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid}",
                timeout=2, verify=False
            )
        except Exception:
            pass  # 刷新失败也不阻塞提交流程

        # 2) 取验证码 + 取 token（JIT）
        captcha = captcha_pool.get_captcha()
        t0 = time.time()
        token, value = session._get_page_token(
            session.url.format(roomid, seatid), require_value=True
        )
        logging.info(f"⚡ 获取token耗时: {time.time() - t0:.2f}s")

        # 3) 提交一次
        success = session.get_submit(
            session.submit_url,
            times=times,
            token=token,
            roomid=roomid,
            seatid=seatid,
            captcha=captcha,
            action=action,
            value=value,
        )

        # 4) 如果失败，快速做一次“全新验证码+token”的二次尝试（最多一次）
        if not success:
            logging.warning("⏩ 首次失败，立即刷新验证码+token 再试一次")
            captcha = session.resolve_captcha()
            token, value = session._get_page_token(
                session.url.format(roomid, seatid), require_value=True
            )
            success = session.get_submit(
                session.submit_url,
                times=times,
                token=token,
                roomid=roomid,
                seatid=seatid,
                captcha=captcha,
                action=action,
                value=value,
            )

        total_time = time.time() - start_time
        if success:
            logging.info(f"🎉 预约成功！总耗时: {total_time:.2f}s")
        else:
            logging.warning(f"❌ 预约失败，总耗时: {total_time:.2f}s")
        return success

    except Exception as e:
        total_time = time.time() - start_time
        logging.error(f"💥 预约异常: {e}，总耗时: {total_time:.2f}s")
        return False

def pre_login_users(users, usernames, passwords, action):
    """提前登录所有用户并预热"""
    logged_sessions = []
    captcha_pools = []
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            logged_sessions.append(None)
            captcha_pools.append(None)
            continue
            
        logging.info(f"User {username}: 提前登录中...")
        
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, msg = s.login(username, password)
        
        if login_success:
            # 连接池调优
            tune_requests_session(s.requests)
            s.requests.headers.update({"Host": "office.chaoxing.com"})

            # Session预热
            warm_up_session(s, roomid, seatid)

            # 只创建验证码池，不立刻预加载（由主定时器统一启动）
            captcha_pool = CaptchaPool(s, CAPTCHA_POOL_SIZE)
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
    
    return logged_sessions, captcha_pools

def wait_until(dt_target):
    """阻塞到目标 datetime（精确到毫秒）"""
    while True:
        now = datetime.datetime.now()
        remain = (dt_target - now).total_seconds()
        if remain <= 0:
            break
        # 剩余大于 0.2s 用 sleep，最后 200ms 用自旋减少调度抖动
        if remain > 0.2:
            time.sleep(remain - 0.2)
        else:
            # 微等待自旋
            pass

def wait_for_target_time(target_time, action, sess_for_time):
    """等待到达目标时间，含服务器时间校正 + 关键窗口静音"""
    # 1) 计算今天/明天目标时间点
    if action:
        base = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        base = datetime.datetime.now()
    tgt = base.replace(
        hour=int(target_time.split(":")[0]),
        minute=int(target_time.split(":")[1]),
        second=int(target_time.split(":")[2]),
        microsecond=0
    )
    if tgt <= base:
        tgt += datetime.timedelta(days=1)

    # 2) 与服务器时间对齐
    offset = 0.0
    if sess_for_time is not None:
        offset = get_server_time_offset(sess_for_time)
    # 校正后的实际等待目标（本地时间域）
    tgt_corrected = tgt - datetime.timedelta(seconds=offset)

    # 3) 关键窗口前提前静音
    now = datetime.datetime.now()
    wait_seconds = (tgt_corrected - now).total_seconds()
    if wait_seconds > 0:
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        # 验证码池在外面按你的 CAPTCH_PRELOAD_TIME 启动，这里只负责到点
        # 进入静音期：提前 QUIET_WINDOW_BEFORE 秒启用内存日志
        quiet_start = tgt_corrected - datetime.timedelta(seconds=QUIET_WINDOW_BEFORE)
        if quiet_start > now:
            time_to_quiet = (quiet_start - now).total_seconds()
            if time_to_quiet > 0:
                time.sleep(time_to_quiet)
        buf_handler.active = True  # 开启缓冲
        # 精细等待到准确目标
        wait_until(tgt_corrected)
    else:
        buf_handler.active = True  # 已经过了，直接缓冲

    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")

def start_reservation_optimized(users, logged_sessions, captcha_pools, action):
    """开始极速预约"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        if s is None or captcha_pool is None:
            continue
        
        logging.info(f"🚀 开始极速预约 - 用户 {username}")
        time_slots = times if isinstance(times[0], list) else [times]
        
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            success = rapid_submit_single(
                s, time_slot, roomid, seatid[0], captcha_pool, action
            )
            if success:
                logging.info(f"✅ 时间段 {time_slot} 预约成功！")
            if i < len(time_slots) - 1:
                time.sleep(0.1)

        captcha_pool.stop()

    # 退出静音窗口，延迟一点再 flush，避免把目标瞬间的打印与业务串扰
    time.sleep(QUIET_WINDOW_AFTER)
    buf_handler.active = False
    buf_handler.flush_buffer()

def main(users, action=False):
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    logged_sessions, captcha_pools = pre_login_users(users, usernames, passwords, action)

    # 统一计算目标时间与启动验证码池的时间
    if action:
        current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    else:
        current_dt = datetime.datetime.now()
    target_dt = current_dt.replace(
        hour=int(RESERVE_TARGET_TIME.split(":")[0]),
        minute=int(RESERVE_TARGET_TIME.split(":")[1]),
        second=int(RESERVE_TARGET_TIME.split(":")[2]),
        microsecond=0
    )
    if target_dt <= current_dt:
        target_dt += datetime.timedelta(days=1)
    start_dt = target_dt - datetime.timedelta(seconds=CAPTCHA_PRELOAD_TIME)
    wait_seconds = (start_dt - current_dt).total_seconds()
    if wait_seconds > 0:
        logging.info(f"将在 {wait_seconds:.1f} 秒后启动验证码池 (目标时间前{CAPTCHA_PRELOAD_TIME}s)")
        time.sleep(wait_seconds)

    # 启动验证码池
    for pool in captcha_pools:
        if pool:
            pool.start_preloading()
    logging.info("✅ 验证码池已启动")

    # 等待到点（含服务器时间校正和静音）
    # 传入一个有效 session 用于时间校正（取第一个可用）
    sess_for_time = None
    for s in logged_sessions:
        if s is not None:
            sess_for_time = s
            break
    wait_for_target_time(RESERVE_TARGET_TIME, action, sess_for_time)
    
    # 到点，开始预约
    start_reservation_optimized(users, logged_sessions, captcha_pools, action)

def debug(users, action=False):
    logging.info(
        f"Global settings: \n"
        f"SLEEPTIME: {SLEEPTIME}\nRESERVE_TARGET_TIME: {RESERVE_TARGET_TIME}\n"
        f"ENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}\n"
        f"CAPTCHA_POOL_SIZE: {CAPTCHA_POOL_SIZE}"
    )
    logging.info(f"Debug Mode start! , action {'on' if action else 'off'}")
    
    if action:
        usernames, passwords = get_user_credentials(action)
    
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        tune_requests_session(s.requests)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        
        captcha_pool = CaptchaPool(s, 3)
        captcha_pool.start_preloading()
        time.sleep(2)
        if isinstance(times[0], list):
            for time_slot in times:
                rapid_submit_single(s, time_slot, roomid, seatid[0], captcha_pool, action)
                time.sleep(0.3)
        else:
            rapid_submit_single(s, times, roomid, seatid[0], captcha_pool, action)
        captcha_pool.stop()
        return

def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    tune_requests_session(s.requests)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)

if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
