import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
for i in range(1, n + 1):
    print("FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else i)
print("Sum: %d" % (n * (n + 1) // 2))
