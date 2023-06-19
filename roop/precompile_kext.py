import subprocess

precompile_commands = []

def set_commands(new_commands):
    global precompile_commands
    precompile_commands = new_commands

def execute_precompile_commands():
    for command in precompile_commands:
        subprocess.run(command)

if __name__ == '__main__':
    execute_precompile_commands()
