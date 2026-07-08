<?php
declare(strict_types=1);

namespace App\S07_LateStaticBinding;

class LsbConsumer
{
    public function exercise(): array
    {
        return [
            UserModel::make(),
            UserModel::whoAmI(),
            UserModel::selfName(),
        ];
    }
}
