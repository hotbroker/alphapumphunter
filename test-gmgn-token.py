import os
from datetime import datetime, timedelta
from loguru import logger
import utils
import alpha_diff_monitor
import time

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

#要把这里的配置放一份到 test_gmgn_cookie_ok 上面带G的变量，不然会出现说指纹不对
def refreshtoken():
    import requests
    cookies = {
        '_did': 'fe74ec9540a255b04ec331434177e5ea',
        '_ga': 'GA1.1.2044110363.1779676776',
        'sid': 'gmgn%7Cdc2e37e44604e7ddab47f5598a247689',
        '_ga_UGLVBMV4Z0': 'GS1.2.1779676784453227.66453a11d073c232b87f1a738beef8e2.R4PgM5O7GTmZVAb91bp3iw%3D%3D.zolpREO8PeyXsWLqpKZWog%3D%3D.7hzi0vRwf2sKjxMjVPJIEA%3D%3D.5yAJ4aZehkUTatpj7wcYcA%3D%3D',
        '__cf_bm': '3t4rl6LkqQwWuluPORXLkG94Qe_x9d2wF6VxBbYpbOU-1779676924.691349-1.0.1.1-A9LxJws.SiacNV1MZtr_RhjcIdi0K6wj5KxW_7WkmEtzbgKx3MSHwf27quj9RBggj.ZcmhA.0coqyjZWO2oSLDCFq4SHYU1HLfzf.UP4E1.mRrc1H8Vd.UeYtcMPX8lL',
        '_ga_0XM0LYXGC8': 'GS2.1.s1779676776$o1$g1$t1779676926$j58$l0$h0',
    }

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9',
        'baggage': 'sentry-environment=production,sentry-release=20260524-347-1a74ea7,sentry-public_key=93c25bab7246077dc3eb85b59d6e7d40,sentry-trace_id=e1db64fc3286422b80e5c550efcc9456,sentry-org_id=4505147559706624,sentry-transaction=%2F,sentry-sampled=false,sentry-sample_rand=0.8634123707393563,sentry-sample_rate=0.0001',
        'content-type': 'application/json',
        'origin': 'https://gmgn.ai',
        'priority': 'u=1, i',
        'referer': 'https://gmgn.ai/?ref=01LaKhx0&chain=sol',
        'sec-ch-ua': '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'sentry-trace': 'e1db64fc3286422b80e5c550efcc9456-9f6ff2a022eb2b9c-0',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        # 'cookie': '_did=fe74ec9540a255b04ec331434177e5ea; _ga=GA1.1.2044110363.1779676776; sid=gmgn%7Cdc2e37e44604e7ddab47f5598a247689; _ga_UGLVBMV4Z0=GS1.2.1779676784453227.66453a11d073c232b87f1a738beef8e2.R4PgM5O7GTmZVAb91bp3iw%3D%3D.zolpREO8PeyXsWLqpKZWog%3D%3D.7hzi0vRwf2sKjxMjVPJIEA%3D%3D.5yAJ4aZehkUTatpj7wcYcA%3D%3D; __cf_bm=3t4rl6LkqQwWuluPORXLkG94Qe_x9d2wF6VxBbYpbOU-1779676924.691349-1.0.1.1-A9LxJws.SiacNV1MZtr_RhjcIdi0K6wj5KxW_7WkmEtzbgKx3MSHwf27quj9RBggj.ZcmhA.0coqyjZWO2oSLDCFq4SHYU1HLfzf.UP4E1.mRrc1H8Vd.UeYtcMPX8lL; _ga_0XM0LYXGC8=GS2.1.s1779676776$o1$g1$t1779676926$j58$l0$h0',
    }

    params = {
        'device_id': '24dafa8c-a698-45db-a039-f69657bb494b',
        'fp_did': '12fbad38e3e0623d8d1a92eb27d5e6af',
        'client_id': 'gmgn_web_20260524-347-1a74ea7',
        'from_app': 'gmgn',
        'app_ver': '20260524-347-1a74ea7',
        'tz_name': 'Asia/Shanghai',
        'tz_offset': '28800',
        'app_lang': 'zh-CN',
        'os': 'web',
        'worker': '0',
    }

    json_data = {
        'refresh_token': "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhZGRyZXNzIjoiMnVjckVtRUd2WkhzMzdzampONFBQdFRRYk5pZ2M1b0xOb1p2NmNnUW1uZkQiLCJhdWQiOiJnbWduLmFpL3JlZnJlc2giLCJjaGFpbiI6InNvbCIsImRhdGEiOnsiYWRkcmVzcyI6IjJ1Y3JFbUVHdlpIczM3c2pqTjRQUHRUUWJOaWdjNW9MTm9adjZjZ1FtbmZEIiwiYXBwIjoiZ21nbiIsImNoYWluIjoic29sIiwiY2xpZW50X2lkIjoiZ21nbl93ZWJfMjAyNjA1MjQtMzQ3LTFhNzRlYTciLCJkZXZpY2VfaWQiOiIyNGRhZmE4Yy1hNjk4LTQ1ZGItYTAzOS1mNjk2NTdiYjQ5NGIiLCJmYXRoZXJfaWQiOiIiLCJmaW5nZXJwcmludCI6InYxMTJjYzA1NjZhMjExYjJlOWZiYmM2OTQ3OGU1NmI1OTEiLCJwbGF0Zm9ybSI6IndlYiIsInVzZXJfaWQiOiI5YmNjOWE3YmQtMjgzZTctMjhkMmYtZWY0MjMtMjU1MDZkMTkifSwiZXhwIjoxNzgyMjcxODc1LCJpYXQiOjE3Nzk2Nzk4NzUsImlzcyI6ImdtZ24uYWkvc2lnbmVyIiwianRpIjoiM2I3ZWZjZjUtZGRkMS00YmNlLWE4NDEtOTE2MDk5YWJmN2Q5IiwibmJmIjoxNzc5Njc5ODc1LCJzdWIiOiJnbWduLmFpL3JlZnJlc2giLCJ2ZXIiOiIxLjAiLCJ2ZXJzaW9uIjoiMi4wIn0.4MBfHbUC4b_nDVBD1J6r-ErrVAGbWS7l0v5HJsgn58hAQa5yDmArr00eCMQYWkB4S15v_EgBLXiwuZRWZK7xIQ"
    }
    url='https://gmgn.ai/account/account/refresh_access_token'
    if utils.is_windows:
        url = url.replace('https://gmgn.ai', 'http://43.163.209.171:8812')

    response = requests.post(
        url,
        params=params,
        cookies=cookies,
        headers=headers,
        json=json_data,
    )
    print(response.text)
    return response
 

last_check_time = 0
def checktoken():
    global last_check_time
    elapsed = time.time() - last_check_time
    if elapsed < 60*10:
        return
    last_check_time = time.time()
    '''
{
  "code": 0,
  "message": "success",
  "data": {
    "data": {
      "expire_at": 1768500046,
      "token": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhZGRyZXNzIjoiMnVjckVtRUd2WkhzMzdzampONFBQdFRRYk5pZ2M1b0xOb1p2NmNnUW1uZkQiLCJhdWQiOiJnbWduLmFpL3JlZnJlc2giLCJjaGFpbiI6InNvbCIsImRhdGEiOnsiYWRkcmVzcyI6IjJ1Y3JFbUVHdlpIczM3c2pqTjRQUHRUUWJOaWdjNW9MTm9adjZjZ1FtbmZEIiwiYXBwIjoiZ21nbiIsImNoYWluIjoic29sIiwiY2xpZW50X2lkIjoiZ21nbl93ZWJfMjAyNjA1MjQtMzQ3LTFhNzRlYTciLCJkZXZpY2VfaWQiOiIyNGRhZmE4Yy1hNjk4LTQ1ZGItYTAzOS1mNjk2NTdiYjQ5NGIiLCJmYXRoZXJfaWQiOiIiLCJmaW5nZXJwcmludCI6InYxMTJjYzA1NjZhMjExYjJlOWZiYmM2OTQ3OGU1NmI1OTEiLCJwbGF0Zm9ybSI6IndlYiIsInVzZXJfaWQiOiI5YmNjOWE3YmQtMjgzZTctMjhkMmYtZWY0MjMtMjU1MDZkMTkifSwiZXhwIjoxNzgyMjcxODc1LCJpYXQiOjE3Nzk2Nzk4NzUsImlzcyI6ImdtZ24uYWkvc2lnbmVyIiwianRpIjoiM2I3ZWZjZjUtZGRkMS00YmNlLWE4NDEtOTE2MDk5YWJmN2Q5IiwibmJmIjoxNzc5Njc5ODc1LCJzdWIiOiJnbWduLmFpL3JlZnJlc2giLCJ2ZXIiOiIxLjAiLCJ2ZXJzaW9uIjoiMi4wIn0.4MBfHbUC4b_nDVBD1J6r-ErrVAGbWS7l0v5HJsgn58hAQa5yDmArr00eCMQYWkB4S15v_EgBLXiwuZRWZK7xIQ"
    },
    "done": true,
    "step": 1
  }
}
'''
    try:
        response = refreshtoken()
        refresh_token = response.json()['data']['data']['token']
        with open('gmgn_authorization.txt', 'w') as f:
            f.write(refresh_token)
    except Exception as e:
        print(e)
        utils.send_notification_feishu(utils.feishu_myself,f'checktoken test gmgn error:{e}', 'test_gmgn_cookie_ok')
while 1:    
    try:
        print(f"im x")
        checktoken()
        with open('gmgn_authorization.txt', 'r') as f:
            gmgn_Bearer = f.read().strip()
        alpha_diff_monitor.GMGN_HEADERS['Authorization'] = f'Bearer {gmgn_Bearer}'
        success = False
        for i in range(3):
            checkgmgn = utils.test_gmgn_cookie_ok(alpha_diff_monitor.GMGN_HEADERS, alpha_diff_monitor.GMGN_COOKIES)
            print(f'status code check {i+1}: {checkgmgn.status_code}')
            if checkgmgn.status_code == 200:
                success = True
                break
            if i < 2:
                time.sleep(5)
        if not success:
            print('cookie is not ok')
            utils.send_notification_feishu(utils.feishu_myself,f'test gmgn error:{checkgmgn.text[:100]}', 'test_gmgn_cookie_ok')
            break
        time.sleep(10)
    except Exception as e:
        utils.send_notification_feishu(utils.feishu_myself,f'test gmgn Exception  error:{e}', 'test_gmgn_cookie_ok')
        print(e)
        time.sleep(10)

