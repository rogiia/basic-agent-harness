RETRY_LIMIT = 3


class ToDoList:
    """
        Helper class to hold a to-do list in memory
    """

    statuses = ["pending", "in_progress", "done", "cancelled", "failed"]

    def __init__(self):
        self._items = []

    def read(self, include_completed=False):
        """Read the to-do list"""
        if include_completed:
            return [item.copy() for item in self._items]
        else:
            return [item.copy() for item in self._items
                    if item["status"] != "done" and item["status"] != "cancelled"]

    def append(self, id, content, status):
        if status not in ToDoList.statuses:
            raise Exception(f"Invalid status {status}. "
                            "Valid to-do statuses: pending, in_progress, done, "
                            "cancelled, failed")
        if self.contains(id):
            raise Exception(f"To do item {id} already exists!")
        new_item = {"id": id, "content": content,
                    "status": status, "retries": 0}
        self._items.append(new_item)
        return new_item.copy()

    def contains(self, id) -> bool:
        """Check if the to do list contains an item with a specific id"""
        for item in self._items:
            if item["id"] == id:
                return True
        return False

    def update(self, id, content, status):
        if status is not None and status not in ToDoList.statuses:
            raise Exception(f"Invalid status {status}. "
                            "Valid to-do statuses: pending, in_progress, done, "
                            "cancelled, failed")
        idx = 0
        while idx < len(self._items):
            if self._items[idx]["id"] == id:
                if content is not None:
                    self._items[idx]["content"] = content
                if status is not None:
                    prev_status = self._items[idx]["status"]
                    self._items[idx]["status"] = status
                    # A failed task being set back to in_progress is a retry attempt.
                    if prev_status == "failed" and status == "in_progress":
                        self._items[idx]["retries"] += 1
                return self._items[idx].copy()
            idx += 1
        raise Exception(f"To do item with id {id} not found")


todo_store = ToDoList()


def todo_append(id, content, status) -> str:
    """Append a new to do item to the to do list"""
    id_str = str(id)
    content_str = str(content)
    status_str = str(status)
    try:
        todo_store.append(id_str, content_str, status_str)
        return f"Successfully appended to do item {id_str} in to do list!"
    except Exception as e:
        return f"Failed to append to do item: {e}"


def todo_list(include_completed=False) -> str:
    """List all the items in the to do list"""
    items = todo_store.read(include_completed)

    result = f"To Do List ({len(items)} items)\n"
    for status in ToDoList.statuses:
        count = sum(1 for i in items if i["status"] == status)
        result += f"{count} {status} items\n"

    result += "-----\n"
    for item in items:
        retry_note = f", {item['retries']
                          } retries" if item["retries"] > 0 else ""
        result += f"- [{item['id']}] {item['content']
                                      } ({item['status']}{retry_note})\n"

    return result


def todo_update(id, content=None, status=None) -> str:
    if content is None and status is None:
        return "No content or status was given to update. Nothing to do."
    try:
        item = todo_store.update(id, content, status)
        retries = item["retries"]
        if item["status"] == "in_progress" and retries > 0:
            if retries >= RETRY_LIMIT:
                return (
                    f"Updated to do item {id} to in_progress — "
                    f"but this is retry {retries} of {
                        RETRY_LIMIT} (retry limit reached). "
                    f"Do not retry again. Escalate to the user instead."
                )
            return (
                f"Successfully updated to do item {id}! "
                f"Retry attempt {retries} of {RETRY_LIMIT}."
            )
        return f"Successfully updated to do item {id}!"
    except Exception as e:
        return f"Failed to update to do item {id}: {e}"
