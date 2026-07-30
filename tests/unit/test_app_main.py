from app.main import main


def test_main_prints_scaffold_ready(capsys) -> None:
    main()

    captured = capsys.readouterr()

    assert captured.out.strip() == "BidMate RAG scaffold is ready."
