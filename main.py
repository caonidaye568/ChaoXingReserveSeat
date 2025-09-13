import json
import time
import argparse
import os
import logging
import datetime
import threading
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

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

# ================= 优化后的全局参数 =================
SLEEPTIME = 0.05  # 进一步减少间隔时间
RESERVE_TARGET_TIME = "22:00:00"  # 预约开始的目标时间（北京时间）
ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 2  # 减少重试次数，专注速度
RESERVE_NEXT_DAY = True  # 预约明天而不是今天的
CAPTCHA_POOL_SIZE = 3  # 减少验证码池大小，避免过期
CAPTCHA_PRELOAD_AT = "21:59:55"  # 验证码池启动时间调整到更接近预约时间
TOKEN_REFRESH_THRESHOLD = 30  # Token刷新阈值(秒)
# ==========================================

class OptimizedCaptchaPool:
    """优化后的验证码缓存池 - 防止验证码过期"""
    def __init__(self, session, pool_size=CAPTCHA_POOL_SIZE):
        self.session = session
        self.pool_size = pool_size
        self.captcha_queue = Queue()
        self.is_active = True
        self.lock = threading.Lock()
        self.last_generate_time = {}
        
    def start_preloading(self):
        """开始预加载验证码 - 更aggressive的刷新策略"""
        logging.info(f"🔄 开始预加载验证码池，目标数量: {self.pool_size}")
        
        def preload_worker():
            while self.is_active:
                try:
                    # 保持池子满，但不超过大小
                    if self.captcha_queue.qsize() < self.pool_size:
                        captcha = self.session.resolve_captcha()
                        if captcha:
                            self.captcha_queue.put({
                                'captcha': captcha,
                                'timestamp': time.time()
                            })
                            logging.info(f"✅ 验证码预加载成功，当前池大小: {self.captcha_queue.qsize()}")
                    
                    # 清理过期验证码(超过20秒的)
                    self._cleanup_expired_captchas()
                    time.sleep(0.3)  # 更频繁的检查
                    
                except Exception as e:
                    logging.warning(f"⚠️ 验证码预加载失败: {e}")
                    time.sleep(0.5)
        
        thread = threading.Thread(target=preload_worker, daemon=True)
        thread.start()
    
    def _cleanup_expired_captchas(self):
        """清理过期的验证码"""
        current_time = time.time()
        temp_queue = Queue()
        
        while not self.captcha_queue.empty():
            captcha_data = self.captcha_queue.get()
            # 如果验证码不到20秒，保留
            if current_time - captcha_data['timestamp'] < 20:
                temp_queue.put(captcha_data)
        
        # 将未过期的验证码放回
        while not temp_queue.empty():
            self.captcha_queue.put(temp_queue.get())
    
    def get_fresh_captcha(self):
        """获取一个新鲜的验证码"""
        # 优先使用池中的验证码
        if not self.captcha_queue.empty():
            captcha_data = self.captcha_queue.get()
            # 检查验证码是否新鲜(15秒内)
            if time.time() - captcha_data['timestamp'] < 15:
                logging.info("🎯 使用池中新鲜验证码")
                return captcha_data['captcha']
        
        # 生成新的验证码
        logging.info("🔄 生成新验证码")
        return self.session.resolve_captcha()
    
    def stop(self):
        """停止预加载"""
        self.is_active = False


def ultra_fast_session_setup(session, roomid, seatid):
    """超快速Session设置 - 最小化延迟"""
    try:
        logging.info("⚡ 超快速Session设置...")
        session.requests.headers.update({
            "Host": "office.chaoxing.com",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache"
        })
        
        # 只做最必要的一次请求来激活session
        session.requests.get(
            f"https://office.chaoxing.com/front/third/apps/seat/code?id={roomid}&seatNum={seatid[0]}", 
            verify=False,
            timeout=3
        )
        logging.info("✅ 超快速Session设置完成")
    except Exception as e:
        logging.warning(f"⚠️ Session设置失败: {e}")


def lightning_submit(session, times, roomid, seatid, captcha_pool, action):
    """闪电提交 - 最小化token过期风险"""
    start_time = time.time()
    
    try:
        # 同时获取验证码和token - 减少总时间
        captcha_start = time.time()
        captcha = captcha_pool.get_fresh_captcha()
        captcha_time = time.time() - captcha_start
        
        token_start = time.time()
        token, value = session._get_page_token(
            session.url.format(roomid, seatid), require_value=True
        )
        token_time = time.time() - token_start
        
        logging.info(f"⚡ 验证码耗时: {captcha_time:.3f}s, Token耗时: {token_time:.3f}s")
        
        # 立即提交，最小化延迟
        submit_start = time.time()
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
        submit_time = time.time() - submit_start
        
        total_time = time.time() - start_time
        
        if success:
            logging.info(f"🎉 闪电预约成功！验证码:{captcha_time:.3f}s + Token:{token_time:.3f}s + 提交:{submit_time:.3f}s = 总计:{total_time:.3f}s")
        else:
            logging.warning(f"❌ 预约失败，总耗时: {total_time:.3f}s")
            
        return success
        
    except Exception as e:
        total_time = time.time() - start_time
        logging.error(f"💥 预约异常: {e}，总耗时: {total_time:.3f}s")
        return False


def pre_login_users_optimized(users, usernames, passwords, action):
    """优化后的用户预登录"""
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
            
        logging.info(f"User {username}: 极速登录中...")
        
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        login_success, msg = s.login(username, password)
        
        if login_success:
            # 延迟到接近预约时间再做session setup
            logged_sessions.append(s)
            captcha_pools.append(OptimizedCaptchaPool(s, CAPTCHA_POOL_SIZE))
        else:
            logging.error(f"User {username} login failed: {msg}")
            logged_sessions.append(None)
            captcha_pools.append(None)
    
    return logged_sessions, captcha_pools


def precise_timing_wait(target_time, action, precision_seconds=0.1):
    """精确时间等待 - 提高到达目标时间的精度"""
    while True:
        current_time = get_current_time(action)
        if current_time >= target_time:
            break
            
        # 计算剩余秒数
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
        
        if wait_seconds > 10:
            logging.info(f"距离目标时间 {target_time} 还有 {wait_seconds:.1f} 秒")
            time.sleep(min(5, wait_seconds - 5))  # 保留5秒做精确等待
        elif wait_seconds > precision_seconds:
            time.sleep(precision_seconds)
        else:
            break
            
    logging.info(f"✅ 精确到达目标时间 {target_time}")


def lightning_reservation(users, logged_sessions, captcha_pools, action):
    """闪电预约 - 极速执行"""
    current_dayofweek = get_current_dayofweek(action)
    
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if current_dayofweek not in daysofweek:
            continue
            
        s = logged_sessions[index]
        captcha_pool = captcha_pools[index]
        if s is None or captcha_pool is None:
            continue
        
        logging.info(f"⚡ 闪电预约启动 - 用户 {username}")
        
        # 在预约前最后一刻进行session设置
        ultra_fast_session_setup(s, roomid, seatid)
        
        time_slots = times if isinstance(times[0], list) else [times]
        
        for i, time_slot in enumerate(time_slots):
            logging.info(f"⚡ 预约时间段 {i+1}/{len(time_slots)}: {time_slot}")
            
            # 最多2次尝试，避免token过期
            for attempt in range(2):
                success = lightning_submit(
                    s, time_slot, roomid, seatid[0], captcha_pool, action
                )
                
                if success:
                    logging.info(f"🎉 时间段 {time_slot} 预约成功！")
                    break
                elif attempt == 0:
                    logging.info(f"🔄 第一次尝试失败，立即重试...")
                    time.sleep(0.05)  # 极短间隔重试
                else:
                    logging.warning(f"❌ 时间段 {time_slot} 两次尝试均失败")
            
            if i < len(time_slots) - 1:
                time.sleep(0.1)
        
        captcha_pool.stop()


def main_optimized(users, action=False):
    logging.info(f"🚀 优化版程序启动 (action={'on' if action else 'off'})")
    
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    
    # 立即登录用户
    logged_sessions, captcha_pools = pre_login_users_optimized(users, usernames, passwords, action)
    
    # 等待到验证码预加载时间(更接近预约时间)
    precise_timing_wait(CAPTCHA_PRELOAD_AT, action)
    
    # 启动验证码池
    for pool in captcha_pools:
        if pool:
            pool.start_preloading()
            time.sleep(0.1)  # 快速启动所有池子
    
    logging.info("🔄 等待5秒让验证码池预热...")
    time.sleep(5)
    
    # 精确等待到预约时间
    precise_timing_wait(RESERVE_TARGET_TIME, action)
    
    # 启动闪电预约
    lightning_reservation(users, logged_sessions, captcha_pools, action)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve - Optimized")
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
    func_dict = {"reserve": main_optimized}  # 使用优化后的main函数
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
