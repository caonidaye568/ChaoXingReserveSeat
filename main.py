import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue

from utils import reserve, get_user_credentials

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

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

SLEEPTIME = 0.05
RESERVE_TARGET_TIME = "19:26:00"
ENABLE_SLIDER = True
MAX_ATTEMPT = 1
RESERVE_NEXT_DAY = False
CAPTCHA_POOL_SIZE = 5
TOKEN_POOL_SIZE = 3  # 新增：token池大小

# ========== 新增 TokenPool ==========
class TokenPool:
    """token 缓存池"""
    def __init__(self, session, roomid, seatid, pool_size=TOKEN_POOL_SIZE):
        self.session = session
        self.roomid = roomid
        self.seatid = seatid
        self.pool_size = pool_size
        self.token_queue = Queue()
        self.is_active = True

    def start_preloading(self):
        logging.info(f"🔄 开始预加载Token池，目标数量: {self.pool_size}")

        def preload_worker():
            while self.is_active and self.token_queue.qsize() < self.pool_size:
                try:
                    token, value = self.session._get_page_token(
                        self.session.url.format(self.roomid, self.seatid), require_value=True
                    )
                    if token:
                        self.token_queue.put((token, value))
                        logging.info(f"✅ Token预加载成功，当前池大小: {self.token_queue.qsize()}")
                    time.sleep(0.5)
                except Exception as e:
                    logging.warning(f"⚠️ Token预加载失败: {e}")
                    time.sleep(1)

        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()

    def get_token(self):
        if not self.token_queue.empty():
            return self.token_queue.get()
        else:
            logging.warning("⚠️ Token池为空，临时获取Token")
            return self.session._get_page_token(
                self.session.url.format(self.roomid, self.seatid), require_value=True
            )

    def stop(self):
        self.is_active = False


class CaptchaPool:
    """验证码缓存池"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        self.lock = threading.Lock()
        
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


def warm_up_session(session, roomid, seatid):
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


def rapid_submit_single(session, times, roomid, seatid, captcha_pool, token_pool, action):
    start_time = time.time()
    try:
        captcha = captcha_pool.get_captcha()
        token, value = token_pool.get_token()  # 从池里拿token
        logging.info(f"⚡ 使用预取的token: {token}")
        
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
    logged_sessions, captcha_pools, token_pools = [], [], []
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
            token_pools.append(None)
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
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            warm_up_session(s, roomid, seatid)
            
            captcha_pool = CaptchaPool(s, CAPTCHA_POOL_SIZE)
            captcha_pool.start_preloading()
            
            token_pool = TokenPool(s, roomid, seatid[0], TOKEN_POOL_SIZE)
            token_pool.start_preloading()
            
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
            token_pools.append(token_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
            token_pools.append(None)
    
    return logged_sessions, captcha_pools, token_pools


def wait_for_target_time(target_time, action):
    current_time = get_current_time(action)
    if current_time < target_time:
        if action:
            current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        else:
            current_dt = datetime.datetime.now()
        target_dt = current_dt.replace(
            hour=int(target_time.split(":")[0]),
            minute=int(target_time.split(":")[1]),
            second=int(target_time.split(":")[2]),
            microsecond=0
        )
        if target_dt <= current_dt:
            target_dt += datetime.timedelta(days=1)
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        time.sleep(wait_seconds)
    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")


def start_reservation_optimized(users, logged_sessions, captcha_pools, token_pools, action):
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if current_dayofweek not in daysofweek:
            continue
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        token_pool = token_pools[index]
        if s is None or captcha_pool is None or token_pool is None:
            continue
        
        logging.info(f"🚀 开始极速预约 - 用户 {username}")
        time_slots = times if isinstance(times[0], list) else [times]
        
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            success = rapid_submit_single(
                s, time_slot, roomid, seatid[0], captcha_pool, token_pool, action
            )
            if success:
                logging.info(f"✅ 时间段 {time_slot} 预约成功！")
            if i < len(time_slots) - 1:
                time.sleep(0.05)
        
        captcha_pool.stop()
        token_pool.stop()


def main(users, action=False):
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    logged_sessions, captcha_pools, token_pools = pre_login_users(users, usernames, passwords, action)
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    start_reservation_optimized(users, logged_sessions, captcha_pools, token_pools, action)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m", "--method", default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a", "--action", action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
