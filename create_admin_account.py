"""
최초 운영자 계정 생성 스크립트.

대시보드에는 회원가입 화면이 없습니다(불특정 다수가 가입하면 안 되는
운영자 전용 시스템). 배포 후 개발자가 이 스크립트를 1회 실행해 최초
계정을 만들고, 이후 비밀번호 변경은 대시보드 내 기능으로 처리합니다.

실행 방법:
  python create_admin_account.py <email> <password>
"""

import sys
from pathlib import Path

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")


def main():
    if len(sys.argv) != 3:
        print("사용법: python create_admin_account.py <email> <password>")
        sys.exit(1)

    email, password = sys.argv[1], sys.argv[2]
    if len(password) < 8:
        print("[오류] 비밀번호는 8자 이상이어야 합니다.")
        sys.exit(1)

    from auth import hash_password
    from storage.admin_store import initialize_admin_db, get_operator_by_email, create_operator

    initialize_admin_db()

    if get_operator_by_email(email):
        print(f"[오류] 이미 존재하는 계정입니다: {email}")
        sys.exit(1)

    create_operator(email, hash_password(password))
    print(f"운영자 계정이 생성되었습니다: {email}")


if __name__ == "__main__":
    main()
