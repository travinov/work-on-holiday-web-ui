    function maskTimeRange(value) {
      const rawValue = value || "";
      const digits = rawValue.replace(/\D/g, "");
      if (digits.length > 8) return rawValue;
      if (!digits) return "";

      const firstHourRaw = digits.slice(0, 2);
      const firstMinuteRaw = digits.slice(2, 4);
      const secondHourRaw = digits.slice(4, 6);
      const secondMinuteRaw = digits.slice(6, 8);

      let result = firstHourRaw;

      if (firstMinuteRaw.length > 0) {
        result += `:${firstMinuteRaw}`;
      }

      if (secondHourRaw.length > 0) {
        result += ` - ${secondHourRaw}`;
      }

      if (secondMinuteRaw.length > 0) {
        result += `:${secondMinuteRaw}`;
      }

      return result;
    }

    function minutesFromTimeRange(value) {
      const match = /^(\d{2}):(\d{2}) - (\d{2}):(\d{2})$/.exec(value || "");
      if (!match) return null;
      const startHour = Number(match[1]);
      const startMinute = Number(match[2]);
      const endHour = Number(match[3]);
      const endMinute = Number(match[4]);
      if (startHour > 23 || endHour > 23 || startMinute > 59 || endMinute > 59) return null;
      const start = startHour * 60 + startMinute;
      let end = endHour * 60 + endMinute;
      if (end === start) return null;
      if (end < start) end += 24 * 60;
      return end - start;
    }

    let validationMessageCounter = 0;

    function addDescribedBy(input, errorId) {
      const ids = new Set((input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
      ids.add(errorId);
      input.setAttribute("aria-describedby", Array.from(ids).join(" "));
    }

    function createValidationMessage(inputs, parent, before = null) {
      const error = document.createElement("div");
      validationMessageCounter += 1;
      error.id = `field-validation-error-${validationMessageCounter}`;
      error.className = "field-validation-error";
      error.setAttribute("aria-live", "polite");
      error.hidden = true;
      parent.insertBefore(error, before);
      inputs.forEach((input) => addDescribedBy(input, error.id));
      return error;
    }

    function setInputValidity(input, message, showError) {
      input.setCustomValidity(message || "");
      if (message && showError) {
        input.setAttribute("aria-invalid", "true");
      } else {
        input.removeAttribute("aria-invalid");
      }
    }

    function renderValidationMessage(error, message, showError) {
      const visibleMessage = showError ? message : "";
      error.textContent = visibleMessage;
      error.hidden = !visibleMessage;
    }

    function durationLabel(totalMinutes) {
      if (totalMinutes === null) return "";
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      return `Итого: ${hours} ч ${String(minutes).padStart(2, "0")} мин`;
    }

    function normalizeTimeInput(input) {
      const previous = input.value;
      const atEnd = input.selectionStart === previous.length && input.selectionEnd === previous.length;
      if (!atEnd || !/^[\d:\s-]*$/.test(previous)) return;
      input.value = maskTimeRange(previous);
    }

    function ensureDurationHint(input) {
      let hint = input.nextElementSibling;
      if (!hint || !hint.classList.contains("duration-hint")) {
        hint = document.createElement("div");
        hint.className = "duration-hint";
        input.insertAdjacentElement("afterend", hint);
      }
      return hint;
    }

    function updateDurationHint(input, totalMinutes) {
      const hint = ensureDurationHint(input);
      hint.textContent = durationLabel(totalMinutes);
      hint.hidden = !hint.textContent;
    }

    function validateTimeRangeValue(value) {
      const match = /^(\d{2}):(\d{2}) - (\d{2}):(\d{2})$/.exec(value || "");
      if (!match) {
        return {
          complete: ((value || "").match(/\d/g) || []).length >= 8,
          message: "Введите время в формате ЧЧ:ММ - ЧЧ:ММ.",
          minutes: null,
          isOvernight: false,
        };
      }

      const minutes = minutesFromTimeRange(value);
      if (minutes === null) {
        const startMinutes = Number(match[1]) * 60 + Number(match[2]);
        const endMinutes = Number(match[3]) * 60 + Number(match[4]);
        const partsInRange = Number(match[1]) <= 23
          && Number(match[2]) <= 59
          && Number(match[3]) <= 23
          && Number(match[4]) <= 59;
        return {
          complete: true,
          message: partsInRange && startMinutes === endMinutes
            ? "Время начала и окончания не должно совпадать."
            : "Укажите корректное время от 00:00 до 23:59.",
          minutes: null,
          isOvernight: false,
        };
      }

      const startMinutes = Number(match[1]) * 60 + Number(match[2]);
      const endMinutes = Number(match[3]) * 60 + Number(match[4]);
      return {
        complete: true,
        message: "",
        minutes,
        isOvernight: endMinutes > 0 && endMinutes < startMinutes,
      };
    }

    function parseClockValue(value) {
      const match = /^(\d{2}):(\d{2})$/.exec(value || "");
      if (!match) return null;
      const hour = Number(match[1]);
      const minute = Number(match[2]);
      if (hour > 23 || minute > 59) return null;
      return hour * 60 + minute;
    }

    function splitStoredTimeRange(value) {
      const match = /^\s*(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})\s*$/.exec(value || "");
      if (!match) return ["", ""];
      return [
        `${match[1].padStart(2, "0")}:${match[2]}`,
        `${match[3].padStart(2, "0")}:${match[4]}`,
      ];
    }

    function formatClockInput(value) {
      const rawValue = value || "";
      const digits = rawValue.replace(/\D/g, "");
      if (digits.length > 4) return rawValue;
      return digits.length > 2 ? `${digits.slice(0, 2)}:${digits.slice(2)}` : digits;
    }

    function nextIsoDate(isoValue) {
      const parsed = isoToDate(isoValue);
      if (!parsed) return "";
      parsed.setDate(parsed.getDate() + 1);
      return dateToIso(parsed);
    }

    function renderOvernightPreview(root, startValue, endValue, isOvernight) {
      const preview = root.querySelector("[data-overnight-preview]");
      if (!preview) return;
      preview.replaceChildren();
      preview.hidden = !isOvernight;
      if (!isOvernight) return;

      const plannedDate = root.closest("form")?.elements.planned_work_date?.value || "";
      const heading = document.createElement("strong");
      heading.textContent = "Будет создано две заявки";
      preview.appendChild(heading);

      const first = document.createElement("div");
      first.textContent = `${isoToRuDate(plannedDate) || "Первая дата"}: ${startValue} - 00:00`;
      preview.appendChild(first);

      const second = document.createElement("div");
      second.textContent = `${isoToRuDate(nextIsoDate(plannedDate)) || "Следующая дата"}: 00:00 - ${endValue}`;
      preview.appendChild(second);
    }

    function attachTimeRangeControl(root) {
      const startInput = root.querySelector("[data-time-start]");
      const endInput = root.querySelector("[data-time-end]");
      const hiddenInput = root.querySelector("[data-time-range-value]");
      const duration = root.querySelector("[data-duration]");
      const lunchWarning = root.querySelector("[data-lunch-warning]");
      if (!startInput || !endInput || !hiddenInput || !duration || !lunchWarning) return;

      const feedback = root.querySelector(".time-feedback") || root;
      const error = createValidationMessage([startInput, endInput], feedback, feedback.firstChild);
      let showAllErrors = false;

      const sync = (forceErrors = false) => {
        showAllErrors = showAllErrors || forceErrors;
        const startMinutes = parseClockValue(startInput.value);
        const endMinutes = parseClockValue(endInput.value);
        const startComplete = /^\d{2}:\d{2}$/.test(startInput.value);
        const endComplete = /^\d{2}:\d{2}$/.test(endInput.value);
        const startMessage = startMinutes === null
          ? (startInput.value ? "Укажите корректное время от 00:00 до 23:59." : "Укажите время начала.")
          : "";
        const endMessage = endMinutes === null
          ? (endInput.value ? "Укажите корректное время от 00:00 до 23:59." : "Укажите время окончания.")
          : "";
        const sameTime = startMinutes !== null && endMinutes !== null && endMinutes === startMinutes;
        const equalMessage = sameTime ? "Время начала и окончания не должно совпадать." : "";
        const isOvernight = startMinutes !== null && endMinutes !== null && endMinutes > 0 && endMinutes < startMinutes;
        const overnightMessage = isOvernight && root.dataset.allowOvernight !== "true"
          ? "Переход через полночь здесь недоступен. Укажите время в пределах одной даты."
          : "";
        const message = equalMessage || overnightMessage || startMessage || endMessage;
        const showStartError = Boolean(startMessage) && (showAllErrors || startComplete);
        const showEndError = Boolean(endMessage) && (showAllErrors || endComplete);
        const showEqualError = Boolean(equalMessage);
        const showOvernightError = Boolean(overnightMessage);

        setInputValidity(
          startInput,
          equalMessage || overnightMessage || startMessage,
          showEqualError || showOvernightError || showStartError,
        );
        setInputValidity(
          endInput,
          equalMessage || overnightMessage || endMessage,
          showEqualError || showOvernightError || showEndError,
        );
        renderValidationMessage(
          error,
          message,
          showEqualError || showOvernightError || showStartError || showEndError,
        );

        if (message) {
          hiddenInput.value = "";
          duration.textContent = "";
          lunchWarning.textContent = "";
          renderOvernightPreview(root, startInput.value, endInput.value, false);
          return;
        }

        const endsAtMidnight = endMinutes === 0 && startMinutes !== 0;
        const effectiveEndMinutes = endsAtMidnight ? 24 * 60 : endMinutes;
        const totalMinutes = isOvernight
          ? 24 * 60 - startMinutes + endMinutes
          : effectiveEndMinutes - startMinutes;

        hiddenInput.value = `${startInput.value} - ${endInput.value}`;
        duration.textContent = durationLabel(totalMinutes);
        const segmentNeedsLunch = isOvernight && root.dataset.allowOvernight === "true"
          ? (24 * 60 - startMinutes >= 300 || endMinutes >= 300)
          : totalMinutes >= 300;
        lunchWarning.textContent = segmentNeedsLunch ? "Из рабочего времени будет вычтен 1 час на обед" : "";
        renderOvernightPreview(root, startInput.value, endInput.value, root.dataset.allowOvernight === "true" && isOvernight);
      };

      const syncFromHidden = () => {
        const [startValue, endValue] = splitStoredTimeRange(hiddenInput.value);
        startInput.value = startValue;
        endInput.value = endValue;
        sync();
      };

      [startInput, endInput].forEach((input) => {
        input.addEventListener("input", () => {
          const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
          if (atEnd && /^[\d:]*$/.test(input.value)) input.value = formatClockInput(input.value);
          sync();
        });
        input.addEventListener("blur", () => sync(true));
        input.addEventListener("change", () => sync(showAllErrors));
        input.addEventListener("invalid", () => sync(true));
      });
      hiddenInput.syncTimeRange = syncFromHidden;
      const form = root.closest("form");
      form?.addEventListener("submit", () => sync(true));
      form?.addEventListener("change", (event) => {
        if (event.target.matches?.('input[data-date-picker="true"]')) sync();
      });
      syncFromHidden();
    }

    function isoToRuDate(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
      return match ? `${match[3]}/${match[2]}/${match[1]}` : (value || "");
    }

    function ruDateToIso(value) {
      const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value || "");
      if (!match) return "";
      const day = Number(match[1]);
      const month = Number(match[2]);
      const year = Number(match[3]);
      if (year < 1000) return "";
      const parsed = new Date(0);
      parsed.setHours(0, 0, 0, 0);
      parsed.setFullYear(year, month - 1, day);
      if (parsed.getFullYear() !== year || parsed.getMonth() !== month - 1 || parsed.getDate() !== day) return "";
      return `${match[3]}-${match[2]}-${match[1]}`;
    }

    function maskRuDate(value) {
      const rawValue = value || "";
      const digits = rawValue.replace(/\D/g, "").slice(0, 8);
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
      const year = Number(match[1]);
      const month = Number(match[2]);
      const day = Number(match[3]);
      if (year < 1000) return null;
      const parsed = new Date(0);
      parsed.setHours(0, 0, 0, 0);
      parsed.setFullYear(year, month - 1, day);
      if (parsed.getFullYear() !== year || parsed.getMonth() !== month - 1 || parsed.getDate() !== day) return null;
      return parsed;
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
      input.maxLength = 10;
      input.pattern = "[0-9]{2}/[0-9]{2}/[0-9]{4}";
      input.inputMode = "numeric";
      input.autocomplete = "off";
      const wrapper = document.createElement("span");
      wrapper.className = "date-input-wrap";
      input.parentNode.insertBefore(wrapper, input);
      wrapper.appendChild(input);

      const pickerButton = document.createElement("button");
      pickerButton.type = "button";
      pickerButton.className = "date-picker-button";
      pickerButton.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18"><path d="M7 2v3M17 2v3M3 9h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
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
      const error = createValidationMessage([input], wrapper.parentNode, wrapper.nextSibling);
      let showAllErrors = false;

      const setIsoValue = (isoValue) => {
        const parsed = isoToDate(isoValue);
        const validIsoValue = parsed ? dateToIso(parsed) : "";
        if (hidden && hidden.type === "hidden") {
          hidden.value = validIsoValue;
        }
        input.value = isoToRuDate(validIsoValue);
        setInputValidity(input, "", false);
        renderValidationMessage(error, "", false);
        if (parsed) visibleMonth = new Date(parsed.getFullYear(), parsed.getMonth(), 1);
      };
      input.setIsoDate = setIsoValue;

      const syncHiddenDate = (forceErrors = false) => {
        showAllErrors = showAllErrors || forceErrors;
        const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
        if (atEnd && /^[\d/]*$/.test(input.value)) input.value = maskRuDate(input.value);
        const isoValue = ruDateToIso(input.value);
        const hasValue = Boolean(input.value);
        const message = isoValue || (!hasValue && !input.required)
          ? ""
          : "Введите реальную дату в формате ДД/ММ/ГГГГ.";
        const complete = ((input.value || "").match(/\d/g) || []).length >= 8;
        const showError = Boolean(message) && (showAllErrors || complete);
        if (hidden && hidden.type === "hidden") {
          hidden.value = message ? "" : isoValue;
        }
        setInputValidity(input, message, showError);
        renderValidationMessage(error, message, showError);
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
            input.dispatchEvent(new Event("change", { bubbles: true }));
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

      input.addEventListener("input", () => syncHiddenDate());
      input.addEventListener("blur", () => syncHiddenDate(true));
      input.addEventListener("change", () => syncHiddenDate(showAllErrors));
      input.addEventListener("invalid", () => syncHiddenDate(true));
      input.form?.addEventListener("submit", () => syncHiddenDate(true));
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

    document.querySelectorAll("[data-time-range]").forEach(attachTimeRangeControl);

    document.querySelectorAll('input[data-time-mask="true"]:not([data-time-part])').forEach((input) => {
      const duration = ensureDurationHint(input);
      const error = createValidationMessage([input], input.parentNode, duration.nextSibling);
      let showAllErrors = false;

      const validate = (forceErrors = false) => {
        showAllErrors = showAllErrors || forceErrors;
        const validation = validateTimeRangeValue(input.value);
        const hasValue = Boolean(input.value);
        const overnightMessage = validation.isOvernight
          && input.dataset.disallowOvernight === "true"
          ? "Ночной интервал нельзя создать одной тестовой заявкой."
          : "";
        const message = overnightMessage
          || (validation.message && (hasValue || input.required) ? validation.message : "");
        const showError = Boolean(message) && (showAllErrors || validation.complete);
        setInputValidity(input, message, showError);
        renderValidationMessage(error, message, showError);
        updateDurationHint(input, message ? null : validation.minutes);
      };

      input.addEventListener("input", () => {
        normalizeTimeInput(input);
        validate();
      });
      input.addEventListener("blur", () => {
        validate(true);
      });
      input.addEventListener("invalid", () => validate(true));
      input.form?.addEventListener("submit", () => {
        validate(true);
      });
      validate();
    });
