import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
import asyncio
import aiohttp

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

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

SLEEPTIME = 0.05  # 进一步减少间隔时间
RESERVE_TARGET_TIME = "22:00:00"  # 预约开始的目标时间（北京时间）
ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 1  # 减少重试次数，专注速度
RESERVE_NEXT_DAY = True  # 预约明天而不是今天的
CAPTCHA_POOL_SIZE = 5  # 增加验证码池大小
TOKEN_POOL_SIZE = 5  # 新增：Token池大小
CAPTCHA_PRELOAD_TIME = 5  # 提前10秒开始预加载

class OptimizedCaptchaPool:
    """优化的验证码+Token缓存池"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.token_queue = Queue()  # 新增Token队列
        self.is_active = True
        self.lock = threading.Lock()
        self.roomid = None
        self.seatid = None
        
    def set_room_info(self, roomid, seatid):
        """设置房间和座位信息"""
        self.roomid = roomid
        self.seatid = seatid
        
    def start_preloading(self):
        """开始预加载验证码和token"""
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        
        def captcha_worker():
            """验证码预加载工作线程"""
            while self.is_active and self.captcha_queue.qsize() < self.pool_size:
                try:
                    captcha = self.session.resolve_captcha()
                    if captcha:
                        self.captcha_queue.put(captcha)
                        logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                    time.sleep(0.2)  # 减少间隔
                except Exception as e:
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(0.3)
        
        def token_worker():
            """Token预加载工作线程"""
            if not self.roomid or not self.seatid:
                return
                
            while self.is_active and self.token_queue.qsize() < TOKEN_POOL_SIZE:
                try:
                    token, value = self.session._get_page_token(
                        self.session.url.format(self.roomid, self.seatid), require_value=True
                    )
                    if token:
                        self.token_queue.put((token, value))
                        logging.info(f"✅ Token预加载成功，当前池大小: {self.token_queue.qsize()}")
                    time.sleep(0.3)  # Token获取间隔稍长
                except Exception as e:
                    logging.warning(f"⚠️ Token预加载失败: {e}")
                    time.sleep(0.5)
        
        # 启动多个预加载线程
        for i in range(2):  # 2个验证码线程
            thread = threading.Thread(target=captcha_worker, daemon=True)
            thread.start()
        
        # 启动Token预加载线程
        thread = threading.Thread(target=token_worker, daemon=True)
        thread.start()
    
    def get_captcha(self):
        """获取一个验证码"""
        if not self.captcha_queue.empty():
            return self.captcha_queue.get()
        else:
            logging.warning("⚠️ 验证码池为空，临时生成验证码")
            return self.session.resolve_captcha()
    
    def get_token(self):
        """获取一个token"""
        if not self.token_queue.empty():
            return self.token_queue.get()
        else:
            logging.warning("⚠️ Token池为空，临时获取token")
            if self.roomid and self.seatid:
                return self.session._get_page_token(
                    self.session.url.format(self.roomid, self.seatid), require_value=True
                )
            return "", ""
    
    def stop(self):
        """停止预加载"""
        self.is_active = False

def warm_up_session(session, roomid, seatid):
    """Session预热 - 模拟正常浏览行为"""
    try:
        logging.info("🔥 开始Session预热...")
        # 访问座位页面
        session.requests.get(
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", 
            verify=False
        )
        time.sleep(0.3)
        
        # 访问其他相关页面
        session.requests.get("https://office.chaoxing.com/data/apps/seat/getusedtimes", verify=False)
        time.sleep(0.3)
        
        logging.info("✅ Session预热完成")
    except Exception as e:
        logging.warning(f"⚠️ Session预热失败: {e}")

def ultra_fast_submit_single(session, times, roomid, seatid, captcha_pool, action):
    """超级快速提交单个预约"""
    start_time = time.time()
    
    try:
        # 1. 并发获取验证码和token
        captcha_start = time.time()
        captcha = captcha_pool.get_captcha()
        
        token_start = time.time()
        token, value = captcha_pool.get_token()
        
        prep_time = time.time() - start_time
        logging.info(f"⚡ 超快获取token: {token} (预备耗时: {prep_time:.2f}s)")
        
        # 2. 立即提交
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

def concurrent_submit_all(session, time_slots, roomid, seatid, captcha_pool, action):
    """并发提交所有时间段"""
    def submit_single_slot(time_slot):
        return ultra_fast_submit_single(session, time_slot, roomid, seatid, captcha_pool, action)
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        # 并发提交所有时间段
        futures = [executor.submit(submit_single_slot, slot) for slot in time_slots]
        
        results = []
        for i, future in enumerate(futures):
            try:
                result = future.result(timeout=30)  # 30秒超时
                results.append((time_slots[i], result))
                if result:
                    logging.info(f"✅ 时间段 {time_slots[i]} 预约成功！")
                else:
                    logging.warning(f"❌ 时间段 {time_slots[i]} 预约失败")
            except Exception as e:
                logging.error(f"💥 时间段 {time_slots[i]} 预约异常: {e}")
                results.append((time_slots[i], False))
    
    return results

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
        
        # 创建预约会话并登录
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
            
            # Session预热
            warm_up_session(s, roomid, seatid)
            
            # 创建优化的验证码池
            captcha_pool = OptimizedCaptchaPool(s, CAPTCHA_POOL_SIZE)
            captcha_pool.set_room_info(roomid, seatid[0])  # 设置房间信息
            
            logged_sessions.append(s)
            captcha_pools.append(captcha_pool)
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
    
    return logged_sessions, captcha_pools

def wait_for_target_time(target_time, action):
    """等待到达目标时间"""
    current_time = get_current_time(action)
    
    if current_time < target_time:
        # 获取当前北京时间
        if action:
            current_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        else:
            current_dt = datetime.datetime.now()
            
        # 计算今天的目标时间
        target_dt = current_dt.replace(
            hour=int(target_time.split(":")[0]),
            minute=int(target_time.split(":")[1]),
            second=int(target_time.split(":")[2]),
            microsecond=0
        )
        
        # 如果目标时间已过，则设为明天
        if target_dt <= current_dt:
            target_dt += datetime.timedelta(days=1)
        
        wait_seconds = (target_dt - current_dt).total_seconds()
        logging.info(f"距离目标时间 {target_time}（北京时间）还有 {wait_seconds:.1f} 秒，sleep……")
        
        time.sleep(wait_seconds)
    
    logging.info(f"到达目标时间 {target_time}（北京时间），开始预约")

def start_reservation_ultra_fast(users, logged_sessions, captcha_pools, action):
    """开始超级快速预约"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        if s is None or captcha_pool is None:
            continue
        
        logging.info(f"🚀 开始超级快速预约 - 用户 {username}")
        
        # 处理时间段
        time_slots = times if isinstance(times[0], list) else [times]
        
        # 并发提交所有时间段
        results = concurrent_submit_all(s, time_slots, roomid, seatid[0], captcha_pool, action)
        
        # 统计结果
        successful_count = sum(1 for _, success in results if success)
        successful_slots = [slot for slot, success in results if success]
        
        if successful_count > 0:
            logging.info(f"🎊 用户 {username} 预约汇总：成功预约了 {successful_count} 个时间段: {successful_slots}")
        else:
            logging.warning(f"😞 用户 {username} 预约汇总：所有时间段预约均失败")
        
        # 停止验证码池
        captcha_pool.stop()

def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"程序启动，立即登录 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    # 提前登录所有用户并预热
    logged_sessions, captcha_pools = pre_login_users(users, usernames, passwords, action)
    
    # 在目标时间前 CAPTCHA_PRELOAD_TIME 秒启动验证码池
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

    for pool in captcha_pools:
        if pool:
            pool.start_preloading()
    logging.info("✅ 验证码池已启动")

    # 等待目标时间
    wait_for_target_time(RESERVE_TARGET_TIME, action)
    
    # 开始超级快速预约
    start_reservation_ultra_fast(users, logged_sessions, captcha_pools, action)

def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nRESERVE_TARGET_TIME: {RESERVE_TARGET_TIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}\nCAPTCHA_POOL_SIZE: {CAPTCHA_POOL_SIZE}"
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
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        
        # 预热并测试超快提交
        captcha_pool = OptimizedCaptchaPool(s, 5)
        captcha_pool.set_room_info(roomid, seatid[0])
        captcha_pool.start_preloading()
        time.sleep(3)  # 等待预加载
        
        # 测试快速预约
        if isinstance(times[0], list):
            results = concurrent_submit_all(s, times, roomid, seatid[0], captcha_pool, action)
            successful_count = sum(1 for _, success in results if success)
            logging.info(f"🔍 调试模式预约汇总：成功预约了 {successful_count} 个时间段")
        else:
            ultra_fast_submit_single(s, times, roomid, seatid[0], captcha_pool, action)
        
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
