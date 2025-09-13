import requests
import time

class ChaoxingAutoSign:
    def __init__(self):
        self.username = "18954239132"
        self.password = "tan333888"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1',
        })

    def encrypt(self, input_text):
        import base64
        from Crypto.Cipher import AES

        key = "u2oh6Vu^HWe4_AES"
        aeskey = key.encode('utf-8')
        iv = key.encode('utf-8')
        cipher = AES.new(aeskey, AES.MODE_CBC, iv)
        pad = lambda s: s + (AES.block_size - len(s) % AES.block_size) * chr(AES.block_size - len(s) % AES.block_size)
        encrypted = cipher.encrypt(pad(input_text).encode('utf-8'))
        return base64.b64encode(encrypted).decode('utf-8')

    def login(self):
        acc = self.encrypt(self.username)
        pwd = self.encrypt(self.password)

        login_url = "https://passport2.chaoxing.com/fanyalogin"
        login_data = {
            'fid': '-1',
            'uname': acc,
            'password': pwd,
            'refer': 'http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Findex',
            't': 'true',
            'forbidotherlogin': 0,
            'validate': 0,
            'doubleFactorLogin': 0,
            'independentId': 0,
        }
        self.session.post(login_url, data=login_data)
        self.session.get('https://office.chaoxing.com/front/third/apps/seat/index')
        print("[+] 登录成功，进入座位系统")

    def get_reserve_list(self):
        today = time.strftime("%Y-%m-%d", time.localtime(time.time() + 8*3600))  # 注意北京时间
        url = "https://office.chaoxing.com/data/apps/seat/reservelist"
        params = {
            'indexId': 0,
            'pageSize': 100,
            'type': -1
        }
        res = self.session.get(url, params=params)
        if res.status_code == 200:
            try:
                data = res.json()["data"]["reserveList"]
                reserve_today = []
                for item in data:
                    if item.get("today", "") == today:
                        reserve_today.append(item)
                return reserve_today
            except Exception as e:
                print(f"[-] 获取预约记录失败: {e}")
                return []
        else:
            print(f"[-] 获取预约请求失败，状态码：{res.status_code}")
            return []

    def sign(self, rid):
        sign_url = f"https://office.chaoxing.com/data/apps/seat/sign?id={rid}"
        res = self.session.get(sign_url)
        if res.status_code == 200:
            try:
                if res.json()["success"]:
                    print(f"[+] 签到成功！预约ID：{rid}")
                else:
                    print(f"[-] 签到失败，返回信息：{res.json()}")
            except Exception as e:
                print(f"[-] 签到请求异常: {e}")
        else:
            print(f"[-] 签到请求失败，状态码：{res.status_code}")

    def wait_until(self, target_time="07:40:00"):
        print(f"[+] 等待签到时间 {target_time} 中...")
        while True:
            current_time = time.strftime("%H:%M:%S", time.localtime(time.time() + 8*3600))
            if current_time >= target_time:
                print(f"[+] 到达签到时间 {target_time}，开始签到")
                break
            print(f"当前时间 {current_time}，等待中...")
            time.sleep(10)

    def run(self):
        self.login()
        self.wait_until(target_time="07:40:00")
        time.sleep(2)
        reserves = self.get_reserve_list()
        if not reserves:
            print("[-] 今天没有预约记录，无法签到")
            return
        target = reserves[0]
        rid = target["id"]
        print(f"[+] 找到预约，ID = {rid}")
        self.sign(rid)

if __name__ == "__main__":
    cxa = ChaoxingAutoSign()
    cxa.run()
