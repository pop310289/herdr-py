import subprocess
import sys

work = sys.argv[1]


def expect(n):
    return ["FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i) for i in range(1, n + 1)] + ["Sum: %d" % (n * (n + 1) // 2)]


for args, n in (([], 15), (["30"], 30), (["1"], 1)):
    p = subprocess.run([sys.executable, "fizz.py"] + args, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
    got = [l.strip() for l in p.stdout.strip().splitlines()]
    if got != expect(n):
        print("fizz.py %s is wrong" % " ".join(args)); sys.exit(1)
print("pass")
