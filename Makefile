VENV	= .venv
PYTHON	= $(VENV)/bin/python

all: run

$(VENV): requirements.txt
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install -r requirements.txt
	touch $(VENV)

install: $(VENV)

run: install
	$(PYTHON) Validation.py

clean:
	rm -rf __pycache__ courbe.png

fclean: clean
	rm -rf $(VENV)

re: fclean all

.PHONY: all install run clean fclean re
