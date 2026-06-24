"""
接雨水问题 (Trapping Rain Water)

题目描述：
给定 n 个非负整数表示每个宽度为 1 的柱子的高度图，计算按此排列的柱子，下雨之后能接多少雨水。

示例：
输入: height = [0,1,0,2,1,0,1,3,2,1,2,1]
输出: 6
解释: 上面是由数组 [0,1,0,2,1,0,1,3,2,1,2,1] 表示的高度图，在这种情况下，可以接 6 个单位的雨水。

LeetCode: https://leetcode.cn/problems/trapping-rain-water/
"""

from typing import List


class Solution:
    """
    解法一：双指针法 (最优解)
    时间复杂度: O(n)
    空间复杂度: O(1)
    """
    def trap(self, height: List[int]) -> int:
        if not height or len(height) < 3:
            return 0
        
        left, right = 0, len(height) - 1
        left_max, right_max = 0, 0
        water = 0
        
        while left < right:
            if height[left] < height[right]:
                # 左边较低，以左边为基准
                if height[left] >= left_max:
                    left_max = height[left]
                else:
                    water += left_max - height[left]
                left += 1
            else:
                # 右边较低或相等，以右边为基准
                if height[right] >= right_max:
                    right_max = height[right]
                else:
                    water += right_max - height[right]
                right -= 1
        
        return water


class SolutionDP:
    """
    解法二：动态规划
    时间复杂度: O(n)
    空间复杂度: O(n)
    
    思路：
    对于每个位置 i，能接的雨水 = min(左边最高, 右边最高) - height[i]
    """
    def trap(self, height: List[int]) -> int:
        if not height or len(height) < 3:
            return 0
        
        n = len(height)
        left_max = [0] * n
        right_max = [0] * n
        
        # 从左向右扫描，记录每个位置左边的最大值
        left_max[0] = height[0]
        for i in range(1, n):
            left_max[i] = max(left_max[i-1], height[i])
        
        # 从右向左扫描，记录每个位置右边的最大值
        right_max[n-1] = height[n-1]
        for i in range(n-2, -1, -1):
            right_max[i] = max(right_max[i+1], height[i])
        
        # 计算每个位置能接的雨水
        water = 0
        for i in range(n):
            water += min(left_max[i], right_max[i]) - height[i]
        
        return water


class SolutionStack:
    """
    解法三：单调栈
    时间复杂度: O(n)
    空间复杂度: O(n)
    
    思路：
    使用单调递减栈，当遇到比栈顶高的柱子时，说明可以形成凹槽接雨水
    """
    def trap(self, height: List[int]) -> int:
        if not height or len(height) < 3:
            return 0
        
        stack = []  # 存储索引，保持单调递减
        water = 0
        
        for i, h in enumerate(height):
            # 当前柱子比栈顶高，可以形成凹槽
            while stack and height[stack[-1]] < h:
                bottom = stack.pop()  # 凹槽底部
                if not stack:
                    break
                # 计算接水量
                left = stack[-1]  # 左边界
                width = i - left - 1
                bounded_height = min(height[left], h) - height[bottom]
                water += width * bounded_height
            
            stack.append(i)
        
        return water


# ==================== 测试代码 ====================

def test_solution():
    """测试所有解法"""
    test_cases = [
        # (输入, 期望输出)
        ([0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1], 6),
        ([4, 2, 0, 3, 2, 5], 9),
        ([1, 2, 3, 4, 5], 0),  # 递增，无法接水
        ([5, 4, 3, 2, 1], 0),  # 递减，无法接水
        ([], 0),
        ([1], 0),
        ([1, 1], 0),
        ([3, 0, 2, 0, 4], 7),
    ]
    
    solutions = [
        ("双指针", Solution()),
        ("动态规划", SolutionDP()),
        ("单调栈", SolutionStack()),
    ]
    
    print("=" * 60)
    print("接雨水问题测试")
    print("=" * 60)
    
    all_passed = True
    for name, sol in solutions:
        print(f"\n【{name}】")
        for i, (height, expected) in enumerate(test_cases):
            result = sol.trap(height)
            status = "✓ PASS" if result == expected else "✗ FAIL"
            if result != expected:
                all_passed = False
            print(f"  测试{i+1}: height={height}")
            print(f"         输出={result}, 期望={expected} {status}")
    
    print("\n" + "=" * 60)
    if all_passed:
        print("所有测试通过！")
    else:
        print("存在失败的测试！")
    print("=" * 60)


if __name__ == "__main__":
    test_solution()
