"""
Manage app logins (stored hashed in users.json). Two roles: ops, admin.

  python manage_users.py list
  python manage_users.py add <username> <role> [password]      # role: ops | admin
  python manage_users.py passwd <username> <newpassword>
  python manage_users.py delete <username>

If you omit the password on `add`, you'll be prompted for it (hidden input).
"""
import getpass
import sys

import auth


def _prompt_password():
    p1 = getpass.getpass("New password: ")
    p2 = getpass.getpass("Confirm password: ")
    if p1 != p2:
        sys.exit("Passwords did not match.")
    if not p1:
        sys.exit("Password cannot be empty.")
    return p1


def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]

    if cmd == "list":
        users = auth.list_users()
        if not users:
            print("No users yet. Add one:  python manage_users.py add <user> <role>")
            return
        width = max(len(u) for u in users)
        for u, role in sorted(users.items()):
            print(f"  {u.ljust(width)}   {role}")
        return

    if cmd == "add":
        if len(argv) < 3:
            sys.exit("Usage: add <username> <role> [password]")
        username, role = argv[1], argv[2]
        password = argv[3] if len(argv) > 3 else _prompt_password()
        auth.set_user(username, password, role)
        print(f"Saved user '{username}' (role: {role}).")
        return

    if cmd == "passwd":
        if len(argv) < 2:
            sys.exit("Usage: passwd <username> [newpassword]")
        username = argv[1]
        role = auth.list_users().get(username)
        if role is None:
            sys.exit(f"No such user: {username}")
        password = argv[2] if len(argv) > 2 else _prompt_password()
        auth.set_user(username, password, role)
        print(f"Password updated for '{username}'.")
        return

    if cmd == "delete":
        if len(argv) < 2:
            sys.exit("Usage: delete <username>")
        print("Deleted." if auth.delete_user(argv[1]) else "No such user.")
        return

    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
