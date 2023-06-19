import subprocess

commands = [
    #['git', 'clone', 'https://github.com/sczhou/CodeFormer.git', '/content/CodeFormer'],    
]

def execute_precompile_commands():
    for command in commands:
        subprocess.run(command)

if __name__ == '__main__':
    execute_precompile_commands()