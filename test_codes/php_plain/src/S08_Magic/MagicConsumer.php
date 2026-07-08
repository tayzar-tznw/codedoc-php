<?php
declare(strict_types=1);

namespace App\S08_Magic;

class MagicConsumer
{
    public function exercise(): array
    {
        $proxy = new EngineProxy(new Engine());

        return [
            $proxy->start(),
            $proxy->temperature,
            EngineFacade::status(),
            $proxy->stop(),
        ];
    }
}
