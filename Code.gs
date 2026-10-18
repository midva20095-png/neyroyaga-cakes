// Сколько кредитов получает НОВЫЙ пользователь. 0 — бесплатных кредитов нет.
var START_CREDITS = 0;

function doPost(e) {
  // АКТИВИРУЕМ БЛОКИРОВКУ СЛУЖБЫ GOOGLE ДЛЯ ЗАЩИТЫ ОТ МНОГОПОТОЧНОСТИ
  var lock = LockService.getScriptLock();
  try {
    // Ожидаем свободного слота до 30 секунд. Если сервер занят, остальные запросы послушно ждут в очереди
    lock.waitLock(30000); 
  } catch (f) {
    return responseJSON({ success: false, error: "Сервер Google перегружен, попробуйте снова" });
  }

  try {
    // ЕДИНСТВЕННОЕ ИЗМЕНЕНИЕ ОТ ВАШЕГО ОРИГИНАЛА: берём ПЕРВЫЙ лист таблицы вместо "активного".
    // getActiveSheet() возвращал тот лист, который оказался активным, поэтому данные искались не там.
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    var data = {};
    
    try {
      data = JSON.parse(e.postData.contents);
    } catch (err) {
      return responseJSON({ success: false, error: "Invalid JSON" });
    }

    var action = data.action;
    var email = data.email ? data.email.toString().trim().toLowerCase() : "";
    var cost = data.cost ? parseInt(data.cost) : 0;
    var amountToAdd = data.amount ? parseInt(data.amount) : 0;
    var password = data.password ? data.password.toString().trim() : "";

    // Оптимизированное чтение таблицы
    var rows = sheet.getDataRange().getValues();
    var userRowIndex = -1;
    var currentCredits = 0;

    // Ищем пользователя по email (исправлена уязвимость пустых ячеек)
    for (var i = 1; i < rows.length; i++) {
      if (rows[i] && rows[i][0]) { // Проверка, что строка и ячейка существуют
        if (rows[i][0].toString().trim().toLowerCase() === email) {
          userRowIndex = i + 1; 
          currentCredits = parseInt(rows[i][2]) || 0;
          break;
        }
      }
    }

    // 1. АВТОРИЗАЦИЯ / РЕГИСТРАЦИЯ
    if (action === "auth") {
      if (userRowIndex !== -1) {
        var storedPassword = rows[userRowIndex - 1][1] ? rows[userRowIndex - 1][1].toString().trim() : "";
        if (storedPassword === password || storedPassword === "") {
          return responseJSON({ success: true, credits: currentCredits });
        } else {
          return responseJSON({ success: false, error: "Неверный пароль" });
        }
      } else {
        var initialCredits = START_CREDITS;
        sheet.appendRow([email, password, initialCredits]);
        SpreadsheetApp.flush(); // Принудительно сохраняем изменения в базу перед снятием лока
        return responseJSON({ success: true, credits: initialCredits });
      }
    }

    // 2. СПИСАНИЕ КРЕДИТОВ (Модели Nano Banana / Flash)
    if (action === "deduct") {
      if (userRowIndex === -1) {
        sheet.appendRow([email, "123456", START_CREDITS]);
        currentCredits = START_CREDITS;
        SpreadsheetApp.flush();
        userRowIndex = sheet.getLastRow();
      }

      if (cost === 0) {
        return responseJSON({ success: true, credits: currentCredits });
      }

      if (currentCredits >= cost) {
        var newBalance = currentCredits - cost;
        sheet.getRange(userRowIndex, 3).setValue(newBalance);
        SpreadsheetApp.flush(); // Сохраняем баланс
        return responseJSON({ success: true, credits: newBalance });
      } else {
        return responseJSON({ success: false, credits: currentCredits, error: "Недостаточно кредитов" });
      }
    }

    // 3. ПОПОЛНЕНИЕ БАЛАНСА (ЮKassa)
    if (action === "add") {
      if (userRowIndex !== -1) {
        var updatedBalance = currentCredits + amountToAdd;
        sheet.getRange(userRowIndex, 3).setValue(updatedBalance);
        SpreadsheetApp.flush();
        return responseJSON({ success: true, credits: updatedBalance });
      } else {
        sheet.appendRow([email, "123456", amountToAdd]);
        SpreadsheetApp.flush();
        return responseJSON({ success: true, credits: amountToAdd });
      }
    }

    return responseJSON({ success: false, error: "Неизвестное действие" });

  } catch (mainError) {
    // В случае любой непредвиденной ошибки возвращаем красивый JSON, чтобы FastAPI не улетал в таймаут
    return responseJSON({ success: false, error: "Критическая ошибка скрипта: " + mainError.toString() });
  } finally {
    // ОБЯЗАТЕЛЬНО ОСВОБОЖДАЕМ ОЧЕРЕДЬ ДЛЯ СЛЕДУЮЩИХ ЗАПРОСОВ
    lock.releaseLock();
  }
}

function responseJSON(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
