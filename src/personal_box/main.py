import uvicorn


def main():
    uvicorn.run("personal_box.app:app", reload=True, port=8765)


if __name__ == "__main__":
    main()
