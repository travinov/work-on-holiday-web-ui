    function clampTimePart(part, max) {
      if (!part) return "";
      const num = Math.min(parseInt(part, 10), max);
      return String(Number.isNaN(num) ? 0 : num).padStart(2, "0");
    }

    function maskTimeRange(value) {
      const digits = (value || "").replace(/\D/g, "").slice(0, 8);
      if (!digits) return "";

      const firstHourRaw = digits.slice(0, 2);
      const firstMinuteRaw = digits.slice(2, 4);
      const secondHourRaw = digits.slice(4, 6);
      const secondMinuteRaw = digits.slice(6, 8);

      let result = firstHourRaw.length === 2 ? clampTimePart(firstHourRaw, 23) : firstHourRaw;

      if (firstMinuteRaw.length > 0) {
        const firstMinute = firstMinuteRaw.length === 2 ? clampTimePart(firstMinuteRaw, 59) : firstMinuteRaw;
        result += `:${firstMinute}`;
      }

      if (secondHourRaw.length > 0) {
        const secondHour = secondHourRaw.length === 2 ? clampTimePart(secondHourRaw, 23) : secondHourRaw;
        result += ` - ${secondHour}`;
      }

      if (secondMinuteRaw.length > 0) {
        const secondMinute = secondMinuteRaw.length === 2 ? clampTimePart(secondMinuteRaw, 59) : secondMinuteRaw;
        result += `:${secondMinute}`;
      }

      return result;
    }

    function minutesFromTimeRange(value) {
      const match = /^\s*(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})\s*$/.exec(value || "");
      if (!match) return null;
      const start = Number(match[1]) * 60 + Number(match[2]);
      let end = Number(match[3]) * 60 + Number(match[4]);
      if (end < start) end += 24 * 60;
      return Math.max(0, end - start);
    }

    function durationLabel(totalMinutes) {
      if (totalMinutes === null) return "";
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      return `Итого: ${hours} ч ${String(minutes).padStart(2, "0")} мин`;
    }

    function normalizeTimeInput(input, force = false) {
      const previous = input.value;
      const atEnd = input.selectionStart === previous.length && input.selectionEnd === previous.length;
      const digitsOnly = /^\d+$/.test(previous.replace(/\s/g, ""));
      if (!force && !atEnd && !digitsOnly) return;
      input.value = maskTimeRange(previous);
    }

    function updateDurationHint(input) {
      let hint = input.nextElementSibling;
      if (!hint || !hint.classList.contains("duration-hint")) {
        hint = document.createElement("div");
        hint.className = "duration-hint";
        input.insertAdjacentElement("afterend", hint);
      }
      hint.textContent = durationLabel(minutesFromTimeRange(input.value));
      hint.hidden = !hint.textContent;
    }

    function isoToRuDate(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
      return match ? `${match[3]}/${match[2]}/${match[1]}` : (value || "");
    }

    function ruDateToIso(value) {
      const digits = (value || "").replace(/\D/g, "").slice(0, 8);
      if (digits.length !== 8) return "";
      const day = digits.slice(0, 2);
      const month = digits.slice(2, 4);
      const year = digits.slice(4, 8);
      return `${year}-${month}-${day}`;
    }

    function maskRuDate(value) {
      const digits = (value || "").replace(/\D/g, "").slice(0, 8);
      const parts = [];
      if (digits.length > 0) parts.push(digits.slice(0, 2));
      if (digits.length > 2) parts.push(digits.slice(2, 4));
      if (digits.length > 4) parts.push(digits.slice(4, 8));
      return parts.join("/");
    }

    const openDatePickers = new Set();
    const monthNames = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
    const weekdayNames = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

    function isoToDate(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
      if (!match) return null;
      return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    }

    function dateToIso(date) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function closeOtherDatePickers(activePopover) {
      openDatePickers.forEach((popover) => {
        if (popover !== activePopover) popover.hidden = true;
      });
      openDatePickers.clear();
      if (activePopover && !activePopover.hidden) openDatePickers.add(activePopover);
    }

    function attachRuDatePicker(input) {
      const hidden = input.previousElementSibling;
      const wrapper = document.createElement("span");
      wrapper.className = "date-input-wrap";
      input.parentNode.insertBefore(wrapper, input);
      wrapper.appendChild(input);

      const pickerButton = document.createElement("button");
      pickerButton.type = "button";
      pickerButton.className = "date-picker-button";
      pickerButton.textContent = "...";
      pickerButton.setAttribute("aria-label", "Выбрать дату из календаря");
      pickerButton.setAttribute("aria-expanded", "false");

      const popover = document.createElement("div");
      popover.className = "date-picker-popover";
      popover.hidden = true;
      popover.setAttribute("role", "dialog");
      popover.setAttribute("aria-label", "Календарь выбора даты");
      popover.addEventListener("click", (event) => {
        event.stopPropagation();
      });

      wrapper.appendChild(pickerButton);
      wrapper.appendChild(popover);

      let visibleMonth = isoToDate(hidden && hidden.type === "hidden" ? hidden.value : "") || new Date();
      visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), 1);

      const setIsoValue = (isoValue) => {
        if (hidden && hidden.type === "hidden") {
          hidden.value = isoValue || "";
        }
        input.value = isoToRuDate(isoValue || "");
      };
      input.setIsoDate = setIsoValue;

      const syncHiddenDate = () => {
        input.value = maskRuDate(input.value);
        const isoValue = ruDateToIso(input.value);
        if (hidden && hidden.type === "hidden") {
          hidden.value = isoValue;
        }
        const parsed = isoToDate(isoValue);
        if (parsed) visibleMonth = new Date(parsed.getFullYear(), parsed.getMonth(), 1);
      };

      const closePicker = () => {
        popover.hidden = true;
        pickerButton.setAttribute("aria-expanded", "false");
        openDatePickers.delete(popover);
      };

      const renderCalendar = () => {
        const selectedIso = hidden && hidden.type === "hidden" ? hidden.value : ruDateToIso(input.value);
        const todayIso = dateToIso(new Date());
        const year = visibleMonth.getFullYear();
        const month = visibleMonth.getMonth();
        const firstDay = new Date(year, month, 1);
        const mondayOffset = (firstDay.getDay() + 6) % 7;
        const gridStart = new Date(year, month, 1 - mondayOffset);

        popover.innerHTML = "";
        const head = document.createElement("div");
        head.className = "date-picker-head";

        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "date-picker-nav";
        prev.textContent = "<";
        prev.setAttribute("aria-label", "Предыдущий месяц");

        const title = document.createElement("div");
        title.className = "date-picker-title";
        title.textContent = `${monthNames[month]} ${year}`;

        const next = document.createElement("button");
        next.type = "button";
        next.className = "date-picker-nav";
        next.textContent = ">";
        next.setAttribute("aria-label", "Следующий месяц");

        head.appendChild(prev);
        head.appendChild(title);
        head.appendChild(next);
        popover.appendChild(head);

        const grid = document.createElement("div");
        grid.className = "date-picker-grid";
        weekdayNames.forEach((name) => {
          const weekday = document.createElement("div");
          weekday.className = "date-picker-weekday";
          weekday.textContent = name;
          grid.appendChild(weekday);
        });

        for (let offset = 0; offset < 42; offset += 1) {
          const current = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + offset);
          const currentIso = dateToIso(current);
          const dayButton = document.createElement("button");
          dayButton.type = "button";
          dayButton.className = "date-picker-day";
          if (current.getMonth() !== month) dayButton.classList.add("outside");
          if (currentIso === todayIso) dayButton.classList.add("today");
          if (currentIso === selectedIso) dayButton.classList.add("selected");
          dayButton.textContent = String(current.getDate());
          dayButton.setAttribute("aria-label", isoToRuDate(currentIso));
          dayButton.addEventListener("click", () => {
            setIsoValue(currentIso);
            closePicker();
            input.focus();
          });
          grid.appendChild(dayButton);
        }

        popover.appendChild(grid);
        prev.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          visibleMonth = new Date(year, month - 1, 1);
          renderCalendar();
        });
        next.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          visibleMonth = new Date(year, month + 1, 1);
          renderCalendar();
        });
      };

      const alignPopover = () => {
        popover.classList.remove("align-right");
        const rect = popover.getBoundingClientRect();
        if (rect.right > window.innerWidth - 12) {
          popover.classList.add("align-right");
        }
      };

      const openPicker = () => {
        syncHiddenDate();
        renderCalendar();
        popover.hidden = false;
        pickerButton.setAttribute("aria-expanded", "true");
        closeOtherDatePickers(popover);
        window.requestAnimationFrame(alignPopover);
      };

      if (hidden && hidden.type === "hidden") {
        setIsoValue(hidden.value);
      }

      input.addEventListener("input", syncHiddenDate);
      input.addEventListener("change", syncHiddenDate);
      input.form?.addEventListener("submit", syncHiddenDate);
      pickerButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (popover.hidden) {
          openPicker();
        } else {
          closePicker();
        }
      });
      document.addEventListener("click", (event) => {
        if (!wrapper.contains(event.target)) closePicker();
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closePicker();
      });
    }

    document.querySelectorAll('input[data-date-picker="true"]').forEach(attachRuDatePicker);

    document.querySelectorAll('input[data-time-mask="true"]').forEach((input) => {
      input.addEventListener("input", () => {
        normalizeTimeInput(input);
        updateDurationHint(input);
      });
      input.addEventListener("blur", () => {
        normalizeTimeInput(input, true);
        updateDurationHint(input);
      });
      input.form?.addEventListener("submit", () => {
        normalizeTimeInput(input, true);
        updateDurationHint(input);
      });
      updateDurationHint(input);
    });
