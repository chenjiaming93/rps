/* global $, Cookies, WebSocket */

$(function () {
  var wsUrl = (window.location.protocol === 'https:' ? 'wss://' : 'ws://') +
      document.location.hostname + ':8443'
  var ws = new WebSocket(wsUrl)
  var wsOnceOpen = false // Whether the websocket was once connected; used in ws.onclose

  // Set this to true when we initiate a close so that we can tell a
  // server-side close and act on it
  var initiatedClose = false

  var $window = $(window)

  var $logonForm = $('#logon-form')
  var $waiting = $('#waiting')
  var $gameContainer = $('#game-container')
  var $pageCover = $('#page-cover')
  var $popOver = $('#popover')

  var me
  var them
  var myScore
  var theirScore
  var turn

  var $userButtons = $('#user-choices-container .button')
  var $opponentButtons = $('#opponent-choices-container .button')

  if ('ontouchstart' in window) {
    $('html').addClass('touch')
  }

  $window.on('beforeunload', function () {
    // Ask for confirmation if in the middle of a game
    if ($gameContainer.is(':visible') && $popOver.is(':hidden')) {
      return 'Are you sure you want to leave the game?'
    } else {
      return undefined
    }
  })

  $window.on('unload', function () {
    // Close websocket connection
    ws.onclose = null
    ws.close()
  })

  var saveUser = function (name) {
    Cookies.set('name', name, { expires: 365 })
  }

  var getUser = function () {
    return Cookies.get('name')
  }

  var forgetUser = function () {
    Cookies.remove('name')
  }

  var sendMessage = function (data) {
    ws.send(JSON.stringify(data))
  }

  var logOn = function (name) {
    sendMessage({action: 'logon', name: name})
    saveUser(name)
  }

  var waitForGame = function () {
    $logonForm.hide()
    $gameContainer.hide()
    $waiting.html('Logged in as <span></span>.<br>Waiting for opponent...')
    $waiting.find('span').text(me)
    $waiting.show()
    sendMessage({action: 'standby'})
  }

  var popOver = function (innerHTML, timeout) {
    $pageCover.show()
    $popOver
      .html(innerHTML)
      .append($('<div class="page-cover"></div>'))
      .fadeIn(function () {
        if (timeout) {
          setTimeout(function () {
            $popOver.fadeOut()
            $pageCover.hide()
          }, timeout)
        }
      })
  }

  $('#quit').click(function () {
    sendMessage({action: 'quit'})
    initiatedClose = true
    ws.close()
    popOver('Successfully quit.<br>Refresh to keep playing.')
  })

  $('#logoff').click(function () {
    sendMessage({action: 'quit'})
    initiatedClose = true
    ws.close()
    forgetUser()
    popOver('Logged off.<br>Refresh to log on again.')
  })

  $('#surrender').click(function () {
    sendMessage({action: 'surrender'})
    popOver('You surrendered!', 3000)
    setTimeout(waitForGame, 3000)
  })

  var failedToConnectMessage = 'Failed to connect to game server.<br>' +
      'Maybe the server is down.<br>' +
      'Please check back later.'
  var connectionDropMessage = 'Connection to game server dropped.<br>' +
      'Please refresh to keep playing.'

  ws.onerror = function () {
    popOver(failedToConnectMessage)
  }

  ws.onclose = function (e) {
    // Stupidly enough, when the websocket can't connect, Safari triggers
    // onclose instead of onerror (http://stackoverflow.com/q/26594331), and
    // it's hard to distinguish that from a dropped connection (e.g.,
    // initiating the connection on an iPhone then putting the phone to sleep);
    // either case the code would be 1006. Therefore, we resort to recording
    // whether the socket was once open.
    if (!wsOnceOpen) {
      popOver(failedToConnectMessage)
    } else if (!initiatedClose) {
      popOver(connectionDropMessage)
    }
  }

  ws.onopen = function () {
    wsOnceOpen = true
    // Try to auto-logon as saved user first
    me = getUser()
    if (me) {
      logOn(me)
      waitForGame()
    } else {
      // Resort to logon form
      $('form').submit(function () {
        me = $.trim($('input[name=name]').val())
        // UTF-8 byte count, courtesy http://stackoverflow.com/a/12205668
        var bytecount = encodeURI(me).split(/%..|./).length - 1
        if (bytecount === 0 || bytecount > 16) {
          window.alert('Name should be between 1 and 16 bytes (UTF-8).')
          return false
        }
        $('input[name=name]').val('')
        logOn(me)
        waitForGame()
        return false
      })
      $('input[type=submit]').css('visibility', '')
    }
  }

  var disableUserButtons = function () {
    $userButtons
      .off('click.buttons')
      .attr('class', 'button disabled')
  }

  var initButtons = function () {
    $userButtons
      .attr('class', 'button clickable')
      .on('click.buttons', function () {
        var $this = $(this)
        disableUserButtons()
        $this.attr('class', 'button selected')

        var move = parseInt($this.attr('data-move'))
        sendMessage({action: 'move', move: move, turn: turn})
      })
    $opponentButtons
      .attr('class', 'button disabled')
  }

  var highlightOpponentMove = function (move) {
    $opponentButtons.filter('[data-move=' + move + ']')
      .attr('class', 'button selected')
  }

  var updateScore = function (id, score) {
    var $elem = $('#' + id)
    $elem.fadeOut(300, function () {
      $elem.text(score).fadeIn(300)
    })
  }

  var countdownRegister
  var startCountdown = function () {
    clearInterval(countdownRegister)
    var remainingSeconds = 9
    var $timer = $('#timer')
    $timer.text(remainingSeconds)
    countdownRegister = setInterval(function () {
      if (remainingSeconds === 0) {
        disableUserButtons()
        clearInterval(countdownRegister)
      } else {
        remainingSeconds--
      }
      $timer.text(remainingSeconds)
    }, 1000)
  }

  var initGame = function () {
    myScore = 0
    theirScore = 0
    turn = 0
    $('#user-name').text(me)
    $('#opponent-name').text(them)
    $('#user-score').text(myScore)
    $('#opponent-score').text(theirScore)
    initButtons()
    startCountdown()
  }

  ws.onmessage = function (ev) {
    var data = JSON.parse(ev.data)
    switch (data.action) {
      case 'match':
        them = data.opponent
        if ($gameContainer.is(':visible')) {
          initGame()
        } else {
          // If not already in game mode, tell the user who they are matched
          // against and wait two seconds before jumping into the game
          $waiting.html('<div id="waiting">Paired with <span></span>.</div>')
          $waiting.find('span').text(them)
          setTimeout(function () {
            $waiting.hide()
            $gameContainer.show()
            initGame()
          }, 2000)
        }
        break

      case 'endturn':
        highlightOpponentMove(data.opponent_move)
        switch (data.winner) {
          case 'me':
            myScore += 1
            updateScore('user-score', myScore)
            break
          case 'them':
            theirScore += 1
            updateScore('opponent-score', theirScore)
            break
          default:
            // Draw, do nothing
            break
        }
        turn += 1
        setTimeout(function () {
          initButtons()
          startCountdown()
        }, 1000)
        break

      case 'endgame':
        var msg
        if (data.winner === 'me') {
          switch (data.reason) {
            case 'leave':
              msg = them + ' left the game.<br>'
              break
            case 'surrender':
              msg = them + ' surrendered.<br>'
              break
            default:
              msg = ''
              break
          }
          msg += 'You won!'
        } else {
          msg = 'You lost!'
        }
        popOver(msg, 3000)
        setTimeout(waitForGame, 3000)
        break

      default:
        console.warn('ignored unknown action ' + data.action)
    }
  }

  // qtip
  $('[title!=""]').qtip({
    style: {
      classes: 'qtip-light qtip-shadow'
    },
    position: {
      my: 'top center',
      at: 'bottom center'
    }
  })
})
