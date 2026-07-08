<?php
declare(strict_types=1);

namespace App\S06_Traits;

class Greeter
{
    use GreetsFormally, GreetsCasually {
        GreetsFormally::hello insteadof GreetsCasually;
        GreetsCasually::hello as bonjour;
    }
}
