import json
import re
import subprocess
import os
import glob

# 配置参数
CONFIG_FILE = 'config.json'
CONFIG_LIST = {
    "linear":{
        "exec_numMotes": [10],
        "conn_phy_mode": ["1M_GFSK", "1M_QPSK", "1M_8PSK", "2M_GFSK", "2M_QPSK", "2M_8PSK", "4M_GFSK", "4M_QPSK", "4M_8PSK"],
        "cycle_start": 0,
        "cycle_end": 10
    },
    "fully_meshed":{
        "exec_numMotes": [10, 50, 100, 300],
        "conn_phy_mode": ["1M_GFSK", "1M_QPSK", "1M_8PSK", "2M_GFSK", "2M_QPSK", "2M_8PSK", "4M_GFSK", "4M_QPSK", "4M_8PSK"],
        "cycle_start": 0,
        "cycle_end": 10 
    },
    "random":{
        "exec_numMotes": [10, 50, 100, 300],
        "conn_phy_mode": ["1M_GFSK", "1M_QPSK", "1M_8PSK", "2M_GFSK", "2M_QPSK", "2M_8PSK", "4M_GFSK", "4M_QPSK", "4M_8PSK"],
        "cycle_start": 0,
        "cycle_end": 10
    }
}
COMMAND = ['python', 'runSim.py']
DATA_DIR = 'simData'  # 数据保存目录

def is_already_done(file_name):
    """
    检测 simData 目录下是否已经存在该规模的模拟结果。
    这里假设生成的文件名中包含 motes 的数量，例如 'simData_motes_10_xxx.dat'
    或者根据目录是否生成了新的文件来判断。
    """
    # 根据 6TiSCH 常见的命名规律搜索文件，例如文件名包含 "motes_10"
    # 你可以根据实际生成的文件名格式调整这个匹配模式
    found_files = glob.glob(file_name)
    
    return len(found_files) > 0

def update_and_run():
    if not os.path.exists(CONFIG_FILE):
        print(f"错误: 找不到 {CONFIG_FILE}")
        return

    for topology in ['linear', 'fully_meshed', 'random']:
        for phy_mode in CONFIG_LIST[topology]['conn_phy_mode']:
            for exec_numMotes in CONFIG_LIST[topology]['exec_numMotes']:
                
                # --- 2. 修改配置 ---
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                data['settings']['combination']['exec_numMotes'] = [exec_numMotes]
                data['settings']['regular']['conn_phy_mode'] = phy_mode
                data['settings']['regular']['conn_deployment_type'] = topology
                
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
                
                print(f"配置文件已更新，准备开始模拟...")

                for cycle in range(CONFIG_LIST[topology]['cycle_start'], CONFIG_LIST[topology]['cycle_end']):
                    print(f"\n检查任务: topology={topology}, phy_mode={phy_mode}, motes={exec_numMotes}, cycle={cycle+1}/{CONFIG_LIST[topology]['cycle_end']-CONFIG_LIST[topology]['cycle_start']}")
                    file_name_pattern = f"./{DATA_DIR}/{topology}/{phy_mode}/{exec_numMotes}/{cycle}"
                    # --- 1. 断点检测逻辑 ---
                    if is_already_done(file_name_pattern):
                        print(f"跳过: 检测到 {DATA_DIR} 中已存在 {file_name_pattern} 的结果文件。")
                        continue
        
                    # --- 3. 执行命令 ---
                    try:
                        # 使用 subprocess.run 会阻塞直到当前模拟完成
                        subprocess.run(COMMAND, check=True)
                        print(f"成功完成模拟: topology={topology}, phy_mode={phy_mode}, motes={exec_numMotes}, cycle={cycle+1}/5")
                    except subprocess.CalledProcessError as e:
                        print(f"运行出错: topology={topology}, phy_mode={phy_mode}, motes={exec_numMotes}, cycle={cycle+1}/5。脚本将停止，你可以修复后重新运行。")
                        quit()
                    except KeyboardInterrupt:
                        print("\n检测到用户中断。下次运行将从当前进度继续。")
                        quit() 

                    # 结果保存
                    # 查看 simData 目录下是否已经存在该规模的模拟结果，如果没有则创建目录并保存结果
                    if not os.path.exists(file_name_pattern):
                        os.makedirs(file_name_pattern, exist_ok=True)
                        # 这里假设 runSim.py 会在当前目录下生成一个结果文件，例如 "result.dat"
                        # 你需要根据实际情况调整这个文件名和路径
                        time_pattern = re.compile(r'\d{8}-\d{6}')
                        subfolders = [ os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR) if time_pattern.search(f) and os.path.exists(os.path.join(DATA_DIR, f)) ]
                        subfolder = max(subfolders, key=os.path.getmtime)
                        # 移动subfolder中的文件到指定目录
                        for file in os.listdir(subfolder):
                            os.rename(os.path.join(subfolder, file), os.path.join(file_name_pattern, file))
                        print(f"结果已保存到 {file_name_pattern}")
                        # 删除临时生成的文件夹
                        os.rmdir(subfolder)


if __name__ == "__main__":
    update_and_run()